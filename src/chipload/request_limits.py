"""Shared HTTP and mutation admission with bounded token-bucket storage."""

import hashlib
import ipaddress
import math
import sqlite3
import time
from typing import Annotated

from pydantic import Field, StrictInt

from .model import Model

# Each request costs one minute of credit; elapsed milliseconds replenish it.
TOKEN = 60_000


class RequestRate(Model):
    per_minute: Annotated[StrictInt, Field(ge=1, le=60_000)]
    burst: Annotated[StrictInt, Field(ge=1, le=10_000)]


class RequestLimits(Model):
    deployment: RequestRate = RequestRate(per_minute=6000, burst=200)
    peer: RequestRate = RequestRate(per_minute=1200, burst=40)
    identity: RequestRate = RequestRate(per_minute=1200, burst=40)
    mutation_identity: RequestRate = RequestRate(per_minute=60, burst=10)
    mutation_shop: RequestRate = RequestRate(per_minute=120, burst=20)
    max_buckets: Annotated[StrictInt, Field(ge=8, le=65_536)] = 4096


def update_request_rates(db, previous: RequestLimits, current: RequestLimits) -> None:
    """Change existing balances in the same transaction as the operator policy."""
    for scope in ("deployment", "peer", "identity", "mutation_identity", "mutation_shop"):
        rate = getattr(current, scope)
        if rate == getattr(previous, scope):
            continue
        capacity = rate.burst * TOKEN
        # Preserve spent credit. The new rate applies to elapsed refill since the
        # last admission; a lower rate cannot leave an old, earlier expiry behind.
        db.execute(
            "UPDATE request_buckets SET credit=MIN(credit,?),per_minute=?,burst=?,"
            "full_at_ms=updated_ms+((?-MIN(credit,?)+?-1)/?) WHERE scope=?",
            (
                capacity,
                rate.per_minute,
                rate.burst,
                capacity,
                capacity,
                rate.per_minute,
                rate.per_minute,
                scope,
            ),
        )


class RateLimited(ValueError):
    def __init__(self, limit: str, retry_after: int):
        self.limit = limit
        self.retry_after = retry_after
        super().__init__("request rate exceeded; retry later")


class RequestCapacity(RuntimeError):
    retry_after = 1

    def __init__(self):
        super().__init__("request admission is at capacity; retry later")


def peer_key(client) -> str:
    """Use the ASGI connection peer, never caller-supplied forwarding headers."""
    try:
        address = ipaddress.ip_address(client[0])
    except (ValueError, TypeError, IndexError):
        return "unknown"
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            return str(address.ipv4_mapped)
        # Rotating interface addresses within a subnet does not create new allowances.
        return str(ipaddress.ip_network((address, 64), strict=False))
    return str(address)


class RequestLimiter:
    def __init__(self, repository, *, clock=None):
        self.repository = repository
        self.clock = clock or (lambda: time.time_ns() // 1_000_000)

    def ingress(self, client) -> None:
        self._admit((("peer", peer_key(client)), ("deployment", "all")))

    def identity(self, subject: str) -> None:
        self._admit((("identity", subject),))

    def mutation(self, subject: str, shop: str) -> None:
        self._admit((("mutation_identity", subject), ("mutation_shop", shop)))

    def _admit(self, selected) -> None:
        # Keep lock waits short so saturation cannot occupy the request pool for seconds.
        # Work after admission runs outside this write transaction.
        try:
            with self.repository.admission.connection(timeout=0.05) as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    limits = self.repository.admission.policy(db).requests
                    at = self.clock()
                    updates = []
                    missing = 0
                    for scope, subject in selected:
                        key = hashlib.sha256(subject.encode()).digest()
                        row = db.execute(
                            "SELECT * FROM request_buckets WHERE scope=? AND subject=?",
                            (scope, key),
                        ).fetchone()
                        rate = getattr(limits, scope)
                        if row is None:
                            credit, updated = rate.burst * TOKEN, at
                            missing += 1
                        else:
                            # A backward wall-clock adjustment never creates refill credit.
                            updated = max(at, row["updated_ms"])
                            credit = min(
                                row["burst"] * TOKEN,
                                row["credit"] + (updated - row["updated_ms"]) * row["per_minute"],
                            )
                            # Policy edits preserve earned credit, capped by the new burst.
                            credit = min(credit, rate.burst * TOKEN)
                        if credit < TOKEN:
                            wait_ms = updated - at + math.ceil((TOKEN - credit) / rate.per_minute)
                            raise RateLimited(scope, max(1, math.ceil(wait_ms / 1000)))
                        credit -= TOKEN
                        full_at = updated + math.ceil(
                            (rate.burst * TOKEN - credit) / rate.per_minute
                        )
                        updates.append(
                            (scope, key, credit, updated, rate.per_minute, rate.burst, full_at)
                        )
                    if missing:
                        # Fully replenished buckets can be removed without restoring extra credit.
                        # Exclude selected rows: they may carry a newly reduced/increased policy.
                        exclusions = " OR ".join("(scope=? AND subject=?)" for _ in updates)
                        keys = tuple(value for update in updates for value in update[:2])
                        db.execute(
                            f"DELETE FROM request_buckets WHERE full_at_ms<=? AND NOT ({exclusions})",
                            (at, *keys),
                        )
                        count = db.execute("SELECT COUNT(*) FROM request_buckets").fetchone()[0]
                        if count + missing > limits.max_buckets:
                            raise RequestCapacity()
                    db.executemany(
                        "INSERT INTO request_buckets VALUES(?,?,?,?,?,?,?) "
                        "ON CONFLICT(scope,subject) DO UPDATE SET "
                        "credit=excluded.credit,updated_ms=excluded.updated_ms,"
                        "per_minute=excluded.per_minute,burst=excluded.burst,"
                        "full_at_ms=excluded.full_at_ms",
                        updates,
                    )
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
        except (sqlite3.Error, OSError) as error:
            # No request is admitted when shared accounting cannot be completed.
            raise RequestCapacity() from error


REQUEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS request_buckets (
    scope TEXT NOT NULL, subject BLOB NOT NULL, credit INTEGER NOT NULL,
    updated_ms INTEGER NOT NULL, per_minute INTEGER NOT NULL, burst INTEGER NOT NULL,
    full_at_ms INTEGER NOT NULL, PRIMARY KEY(scope,subject)
);
CREATE INDEX IF NOT EXISTS request_buckets_expiry ON request_buckets(full_at_ms);
"""
