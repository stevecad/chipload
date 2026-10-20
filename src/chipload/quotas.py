"""Durable platform admission policy and source/build quota accounting."""

import math
from datetime import datetime, timedelta
from typing import Annotated

from pydantic import Field, StrictInt

from .model import Model
from .project import MAX_OUTPUT_BYTES
from .request_limits import RequestLimits

Count = Annotated[StrictInt, Field(ge=1, le=1_000_000)]
ByteLimit = Annotated[StrictInt, Field(ge=1, le=1024**4)]


class ScopeQuota(Model):
    pending_builds: Count
    running_builds: Annotated[StrictInt, Field(ge=1, le=128)]
    builds_per_hour: Count
    retained_builds: Count
    report_bytes: ByteLimit
    retained_sources: Count
    source_bytes: ByteLimit


class QuotaPolicy(Model):
    requests: RequestLimits = RequestLimits()
    deployment: ScopeQuota = ScopeQuota(
        pending_builds=100,
        running_builds=2,
        builds_per_hour=600,
        retained_builds=10_000,
        report_bytes=2 * 1024**3,
        retained_sources=10_000,
        source_bytes=1024**3,
    )
    shop: ScopeQuota = ScopeQuota(
        pending_builds=10,
        running_builds=1,
        builds_per_hour=60,
        retained_builds=1000,
        report_bytes=256 * 1024**2,
        retained_sources=1000,
        source_bytes=128 * 1024**2,
    )
    identity: ScopeQuota = ScopeQuota(
        pending_builds=20,
        running_builds=1,
        builds_per_hour=120,
        retained_builds=2000,
        report_bytes=512 * 1024**2,
        retained_sources=2000,
        source_bytes=256 * 1024**2,
    )


class QuotaExceeded(ValueError):
    def __init__(self, quota: str, *, retry_after: int | None = None):
        self.quota = quota
        self.retry_after = retry_after
        super().__init__(
            f"{quota} quota exceeded; "
            + (
                "retry later"
                if retry_after
                else "operator capacity or retention action is required"
            )
        )


def scopes(policy: QuotaPolicy, shop: str, actor: str):
    # SQL fragments are fixed here; all shop/identity values stay parameters.
    return (
        ("deployment", policy.deployment, "", ()),
        ("shop", policy.shop, "WHERE shop_id=?", (shop,)),
        ("identity", policy.identity, "WHERE actor=?", (actor,)),
    )


def admit_source(db, policy: QuotaPolicy, shop: str, actor: str, size: int) -> None:
    for scope, limit, where, parameters in scopes(policy, shop, actor):
        used = db.execute(
            f"SELECT COUNT(*) AS records, COALESCE(SUM(length(project)),0) AS bytes "
            f"FROM sources {where}",
            parameters,
        ).fetchone()
        if used["records"] >= limit.retained_sources:
            raise QuotaExceeded(f"{scope}.retained_sources")
        if used["bytes"] + size > limit.source_bytes:
            raise QuotaExceeded(f"{scope}.source_bytes")


def admit_build(db, policy: QuotaPolicy, shop: str, actor: str, at: datetime) -> None:
    since = (at - timedelta(hours=1)).isoformat()
    for scope, limit, where, parameters in scopes(policy, shop, actor):
        used = db.execute(
            "SELECT COUNT(*) AS records, "
            "COALESCE(SUM(state IN ('queued','running')),0) AS pending, "
            "COALESCE(SUM(created_at>?),0) AS recent, "
            "MIN(CASE WHEN created_at>? THEN created_at END) AS first_recent, "
            "COALESCE(SUM(CASE WHEN state IN ('queued','running') "
            "THEN ? ELSE COALESCE(length(report),0) END),0) AS bytes "
            f"FROM builds {where}",
            (since, since, MAX_OUTPUT_BYTES, *parameters),
        ).fetchone()
        if used["pending"] >= limit.pending_builds:
            raise QuotaExceeded(f"{scope}.pending_builds", retry_after=5)
        if used["recent"] >= limit.builds_per_hour:
            expires = datetime.fromisoformat(used["first_recent"]) + timedelta(hours=1)
            raise QuotaExceeded(
                f"{scope}.builds_per_hour",
                retry_after=max(1, math.ceil((expires - at).total_seconds())),
            )
        if used["records"] >= limit.retained_builds:
            raise QuotaExceeded(f"{scope}.retained_builds")
        # Reserve the entire possible output before admitting executable work.
        if used["bytes"] + MAX_OUTPUT_BYTES > limit.report_bytes:
            raise QuotaExceeded(f"{scope}.report_bytes")


def admit_running(db, policy: QuotaPolicy, shop: str, actor: str) -> None:
    for scope, limit, where, parameters in scopes(policy, shop, actor):
        clause = where + " AND " if where else "WHERE "
        running = db.execute(
            f"SELECT COUNT(*) FROM builds {clause}state='running'", parameters
        ).fetchone()[0]
        # Expired leases still occupy slots until their containers are removed.
        if running >= limit.running_builds:
            raise QuotaExceeded(f"{scope}.running_builds", retry_after=5)


def quota_usage(db, *, at: datetime, shop: str | None = None, identity: str | None = None) -> dict:
    """Operator-visible usage, including full reservations for unfinished builds."""
    selected = [("deployment", "", ())]
    if shop is not None:
        selected.append(("shop", "WHERE shop_id=?", (shop,)))
    if identity is not None:
        selected.append(("identity", "WHERE actor=?", (identity,)))
    result = {}
    for scope, where, parameters in selected:
        builds = db.execute(
            "SELECT COUNT(*) AS retained_builds, "
            "COALESCE(SUM(state IN ('queued','running')),0) AS pending_builds, "
            "COALESCE(SUM(state='running'),0) AS running_builds, "
            "COALESCE(SUM(created_at>?),0) AS builds_last_hour, "
            "COALESCE(SUM(CASE WHEN state IN ('queued','running') "
            "THEN ? ELSE COALESCE(length(report),0) END),0) AS report_bytes_reserved_or_stored "
            f"FROM builds {where}",
            ((at - timedelta(hours=1)).isoformat(), MAX_OUTPUT_BYTES, *parameters),
        ).fetchone()
        sources = db.execute(
            "SELECT COUNT(*) AS retained_sources, COALESCE(SUM(length(project)),0) AS source_bytes "
            f"FROM sources {where}",
            parameters,
        ).fetchone()
        result[scope] = {**dict(builds), **dict(sources)}
    result["deployment"]["request_buckets"] = db.execute(
        "SELECT COUNT(*) FROM request_buckets"
    ).fetchone()[0]
    return result


QUOTA_SCHEMA = """
CREATE INDEX IF NOT EXISTS builds_by_actor ON builds(actor,created_at);
CREATE INDEX IF NOT EXISTS builds_by_state_shop ON builds(state,shop_id,actor);
CREATE INDEX IF NOT EXISTS sources_by_actor ON sources(actor);
CREATE TABLE IF NOT EXISTS quota_policy (
    id INTEGER PRIMARY KEY CHECK(id=1), generation INTEGER NOT NULL, policy BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS quota_changes (
    generation INTEGER PRIMARY KEY, policy BLOB NOT NULL, time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatch_clock (
    id INTEGER PRIMARY KEY CHECK(id=1), turn INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatch_turns (
    scope TEXT NOT NULL CHECK(scope IN ('shop','identity')), subject TEXT NOT NULL,
    turn INTEGER NOT NULL, PRIMARY KEY(scope,subject)
);
"""
