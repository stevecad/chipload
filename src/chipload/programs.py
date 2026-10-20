"""Durable admission and inspection of bounded, caller-scoped Python runs."""

import math
import uuid
from datetime import datetime, timedelta

from .access import Grant, Principal, require
from .artifact import canonical_json, digest
from .lifecycle import Conflict, IntegrityError, NotFound, now
from .program_model import MAX_CALLS, MAX_PROGRAM_BYTES, PROGRAM_RESERVATION, RunPython, RunRecord
from .project import unique_json
from .quotas import QuotaExceeded, scopes


def scoped(principal, shop):
    return Principal(
        subject=principal.subject, grants=tuple(g for g in principal.grants if g.shop_id == shop)
    )


def intersect(ceiling, current):
    if current is None or ceiling.subject != current.subject:
        return Principal(subject=ceiling.subject)
    grants = []
    for before in ceiling.grants:
        for live in current.grants:
            if before.shop_id != live.shop_id:
                continue
            actions = tuple(a for a in before.actions if a in live.actions)
            if not actions:
                continue
            operations = live.operations if before.operations is None else before.operations
            if before.operations is not None and live.operations is not None:
                operations = tuple(op for op in before.operations if op in live.operations)
            grants.append(Grant(shop_id=before.shop_id, actions=actions, operations=operations))
    return Principal(subject=ceiling.subject, grants=tuple(grants))


def decode(row):
    if row is None:
        raise NotFound("Python run not found")
    try:
        payload = bytes(row["request"])
        request = RunPython.model_validate(unique_json(payload))
        if (
            len(payload) > MAX_PROGRAM_BYTES
            or digest(payload) != row["request_sha256"]
            or request.shop_id != row["shop_id"]
            or request.request_key != row["request_key"]
        ):
            raise ValueError("run request integrity failure")
        result = unique_json(row["result"]) if row["result"] else {}
        calls = unique_json(row["calls"])
        ceiling = Principal.model_validate(unique_json(row["ceiling"]))
        if (
            len(row["ceiling"]) > 64 * 1024
            or ceiling.subject != row["actor"]
            or any(g.shop_id != row["shop_id"] for g in ceiling.grants)
        ):
            raise ValueError("invalid Python grant ceiling")
        if (row["state"] in {"queued", "running"}) != (row["finished_at"] is None):
            raise ValueError("invalid Python completion timestamp")
        if row["state"] == "running" and not row["claim"]:
            raise ValueError("missing Python worker claim")
        if not isinstance(result, dict) or not isinstance(calls, list) or len(calls) > MAX_CALLS:
            raise ValueError("run result integrity failure")
        if (
            len(payload) + len(row["ceiling"]) + len(row["calls"]) + len(row["result"] or b"")
            > PROGRAM_RESERVATION
        ):
            raise ValueError("run storage exceeds its reservation")
        return RunRecord(
            id=row["id"],
            shop_id=row["shop_id"],
            actor=row["actor"],
            request_sha256=row["request_sha256"],
            state=row["state"],
            created_at=row["created_at"],
            deadline=row["deadline"],
            finished_at=row["finished_at"],
            cancel_requested=bool(row["cancel_requested"]),
            calls=calls,
            **result,
        )
    except (ValueError, TypeError) as error:
        raise IntegrityError("invalid stored Python run") from error


class ProgramService:
    def __init__(self, repository, *, clock=now):
        self.repository, self.clock = repository, clock

    def submit(self, principal, request):
        request = RunPython.model_validate(request)
        who = require(principal, request.shop_id, "code:run")
        ceiling = canonical_json(scoped(who, request.shop_id))
        if len(ceiling) > 64 * 1024:
            raise ValueError("Python execution grant exceeds its byte limit")
        payload, at = canonical_json(request), self.clock()
        with self.repository.transaction() as db:
            prior = db.execute(
                "SELECT * FROM python_runs WHERE shop_id=? AND actor=? AND request_key=?",
                (request.shop_id, who.subject, request.request_key),
            ).fetchone()
            if prior:
                if bytes(prior["request"]) != payload:
                    raise Conflict("request key already identifies a different Python program")
                return decode(prior)
            for scope, limit, where, parameters in scopes(
                self.repository.policy(), request.shop_id, who.subject
            ):
                since = (at - timedelta(hours=1)).isoformat()
                used = db.execute(
                    "SELECT COUNT(*) AS records, COALESCE(SUM(state IN ('queued','running')),0) AS pending, "
                    "COALESCE(SUM(created_at>?),0) AS recent, MIN(CASE WHEN created_at>? THEN created_at END) AS first "
                    f"FROM python_runs {where}",
                    (since, since, *parameters),
                ).fetchone()
                if used["pending"] >= limit.pending_programs:
                    raise QuotaExceeded(f"{scope}.pending_programs", retry_after=5)
                if used["recent"] >= limit.programs_per_hour:
                    expiry = datetime.fromisoformat(used["first"]) + timedelta(hours=1)
                    raise QuotaExceeded(
                        f"{scope}.programs_per_hour",
                        retry_after=max(1, math.ceil((expiry - at).total_seconds())),
                    )
                if used["records"] >= limit.retained_programs:
                    raise QuotaExceeded(f"{scope}.retained_programs")
                if (used["records"] + 1) * PROGRAM_RESERVATION > limit.program_bytes:
                    raise QuotaExceeded(f"{scope}.program_bytes")
            identifier = uuid.uuid4().hex
            db.execute("INSERT OR IGNORE INTO shops(id) VALUES(?)", (request.shop_id,))
            db.execute(
                "INSERT INTO python_runs(id,shop_id,actor,request_key,request,request_sha256,ceiling,state,created_at,deadline,calls) "
                "VALUES(?,?,?,?,?,?,?,'queued',?,?,?)",
                (
                    identifier,
                    request.shop_id,
                    who.subject,
                    request.request_key,
                    payload,
                    digest(payload),
                    ceiling,
                    at.isoformat(),
                    (at + timedelta(minutes=5)).isoformat(),
                    b"[]",
                ),
            )
            self.repository.event(db, request.shop_id, who.subject, "code.requested", identifier)
            return decode(
                db.execute("SELECT * FROM python_runs WHERE id=?", (identifier,)).fetchone()
            )

    def read(self, principal, shop, identifier):
        require(principal, shop, "code:read")
        with self.repository.connection() as db:
            return decode(
                db.execute(
                    "SELECT * FROM python_runs WHERE id=? AND shop_id=?", (identifier, shop)
                ).fetchone()
            )

    def cancel(self, principal, shop, identifier):
        who = require(principal, shop, "code:cancel")
        with self.repository.transaction() as db:
            row = db.execute(
                "SELECT * FROM python_runs WHERE id=? AND shop_id=?", (identifier, shop)
            ).fetchone()
            record = decode(row)
            if record.state == "queued":
                db.execute(
                    "UPDATE python_runs SET state='cancelled',cancel_requested=1,finished_at=? WHERE id=?",
                    (self.clock().isoformat(), identifier),
                )
            elif record.state == "running":
                db.execute("UPDATE python_runs SET cancel_requested=1 WHERE id=?", (identifier,))
            else:
                return record
            self.repository.event(db, shop, who.subject, "code.cancel-requested", identifier)
            return decode(
                db.execute("SELECT * FROM python_runs WHERE id=?", (identifier,)).fetchone()
            )
