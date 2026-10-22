"""Durable bounded handoff from caller-scoped SDK/REST to a trusted connector."""

import uuid
from datetime import datetime, timezone

from .access import require
from .artifact import canonical_json
from .client import TransportError
from .directory_model import SearchPage
from .lifecycle import Conflict, NotFound, now
from .limits import bounded_json
from .network_model import (
    MAX_NETWORK_REQUEST,
    MAX_NETWORK_RESULT,
    NETWORK_RETENTION_SECONDS,
    NetworkComplete,
    NetworkError,
    NetworkLease,
    NetworkRecord,
    NetworkSubmit,
    search_hash,
)
from .peer import checked_reply
from .peer_model import PeerResponse
from .quotas import QuotaExceeded
from .request_limits import RequestLimiter


def at(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc)


class NetworkService:
    def __init__(self, repository, *, clock=now):
        self.repository, self.clock = repository, clock
        self.limiter = RequestLimiter(repository)

    def expire(self, db, when):
        db.execute("UPDATE network_jobs SET state='expired',finished=?,claim=NULL,dispatcher=NULL,"
                   "error=? WHERE state IN ('queued','running') AND deadline<=?", (
                       when, canonical_json({"code": "network_deadline", "status": 504}), when))

    @staticmethod
    def record(row):
        return NetworkRecord(id=row["id"], shop_id=row["shop_id"], state=row["state"],
            created_at=at(row["created"]), deadline=at(row["deadline"]),
            finished_at=at(row["finished"]) if row["finished"] is not None else None,
            result=bounded_json(bytes(row["result"])) if row["result"] is not None else None,
            error=bounded_json(bytes(row["error"])) if row["error"] is not None else None)

    def submit(self, principal, request):
        request = NetworkSubmit.model_validate(request)
        who = require(principal, request.shop_id, "network:query")
        encoded = canonical_json(request)
        if len(encoded) > MAX_NETWORK_REQUEST:
            raise ValueError("network intent exceeds its byte limit")
        with self.repository.transaction() as db:
            when = self.clock().timestamp()
            self.expire(db, when)
            row = db.execute("SELECT * FROM network_jobs WHERE shop_id=? AND actor=? AND request_key=?",
                             (request.shop_id, who.subject, request.request_key)).fetchone()
            if row is not None:
                if bytes(row["intent"]) != encoded:
                    raise Conflict("network request key already identifies another intent")
                return self.record(row)
            remaining = request.deadline.timestamp() - when
            if not 0 < remaining <= 30:
                raise ValueError("network deadline must be within thirty seconds")
            if request.query.kind != "search" and request.query.request.deadline != request.deadline:
                raise ValueError("network and peer deadlines must agree")
            if db.execute("SELECT 1 FROM shops WHERE id=?", (request.shop_id,)).fetchone() is None:
                raise NotFound("shop not found")
            db.execute("DELETE FROM network_jobs WHERE finished IS NOT NULL AND finished<?",
                       (when - NETWORK_RETENTION_SECONDS,))
            for where, values, pending_limit, byte_limit in (
                ("1", (), 32, 128 * 1024**2),
                ("actor=?", (who.subject,), 8, 32 * 1024**2),
                ("shop_id=?", (request.shop_id,), 16, 64 * 1024**2),
            ):
                counts = db.execute("SELECT count(*),sum(state IN ('queued','running')),"
                    "coalesce(sum(length(intent)+CASE WHEN state IN ('queued','running') THEN ? ELSE coalesce(length(result),0)+coalesce(length(error),0) END),0) "
                    "FROM network_jobs WHERE " + where, (MAX_NETWORK_RESULT, *values)).fetchone()
                if counts[0] >= 2048 or (counts[1] or 0) >= pending_limit or counts[2] + len(encoded) + MAX_NETWORK_RESULT > byte_limit:
                    raise QuotaExceeded("network_capacity", retry_after=30)
            identifier = uuid.uuid4().hex
            db.execute("INSERT INTO network_jobs(id,shop_id,actor,request_key,intent,state,created,deadline) VALUES(?,?,?,?,?,'queued',?,?)",
                (identifier, request.shop_id, who.subject, request.request_key, encoded, when, request.deadline.timestamp()))
            return self.record(db.execute("SELECT * FROM network_jobs WHERE id=?", (identifier,)).fetchone())

    def read(self, principal, shop, identifier):
        who = require(principal, shop, "network:query")
        with self.repository.transaction() as db:
            self.expire(db, self.clock().timestamp())
            row = db.execute("SELECT * FROM network_jobs WHERE id=? AND shop_id=? AND actor=?",
                             (identifier, shop, who.subject)).fetchone()
            if row is None:
                raise NotFound("network job not found")
            return self.record(row)

    def cancel(self, principal, shop, identifier):
        who = require(principal, shop, "network:query")
        with self.repository.transaction() as db:
            when = self.clock().timestamp()
            self.expire(db, when)
            row = db.execute("SELECT * FROM network_jobs WHERE id=? AND shop_id=? AND actor=?",
                             (identifier, shop, who.subject)).fetchone()
            if row is None:
                raise NotFound("network job not found")
            if row["state"] in {"queued", "running"}:
                db.execute("UPDATE network_jobs SET state='cancelled',finished=?,claim=NULL,dispatcher=NULL WHERE id=?", (when, identifier))
            return self.record(db.execute("SELECT * FROM network_jobs WHERE id=?", (identifier,)).fetchone())

    def claim(self, principal, shop):
        who = require(principal, shop, "network:dispatch")
        with self.repository.transaction() as db:
            self.expire(db, self.clock().timestamp())
            if db.execute("SELECT count(*) FROM network_jobs WHERE state='running'").fetchone()[0] >= 4:
                return {"lease": None}
            row = db.execute("SELECT n.* FROM network_jobs n WHERE n.shop_id=? AND n.state='queued' "
                "AND (SELECT count(*) FROM network_jobs r WHERE r.state='running' AND r.actor=n.actor)<2 "
                "ORDER BY n.created,n.id LIMIT 1", (shop,)).fetchone()
            if row is None:
                return {"lease": None}
            self.limiter.mutation(who.subject, shop)
            claim = uuid.uuid4().hex
            db.execute("UPDATE network_jobs SET state='running',claim=?,dispatcher=? WHERE id=?", (claim, who.subject, row["id"]))
            lease = NetworkLease(shop_id=shop, job_id=row["id"], claim=claim,
                                 request=NetworkSubmit.model_validate(bounded_json(bytes(row["intent"]))))
            return {"lease": lease.model_dump(mode="json")}

    def complete(self, principal, completion):
        completion = NetworkComplete.model_validate(completion)
        who = require(principal, completion.shop_id, "network:dispatch")
        if (completion.result is None) == (completion.error is None):
            raise ValueError("network completion requires exactly one result or error")
        # Read-only requests are never reclaimed/replayed after a lost connector.
        # Rechecking under the write lock fences cancellation and concurrent replies.
        with self.repository.transaction() as db:
            when = self.clock()
            self.expire(db, when.timestamp())
            row = db.execute("SELECT * FROM network_jobs WHERE id=? AND shop_id=? AND dispatcher=?",
                (completion.job_id, completion.shop_id, who.subject)).fetchone()
            if row is None:
                raise NotFound("network delivery not found")
            if row["claim"] != completion.claim or row["state"] != "running":
                raise Conflict("network delivery is no longer active")
            intent = NetworkSubmit.model_validate(bounded_json(bytes(row["intent"])))
            result, error = completion.result, completion.error
            if result is not None:
                if intent.query.kind == "search":
                    if (not isinstance(result, SearchPage) or result.directory_id != intent.query.directory_id
                            or result.query_sha256 != search_hash(intent.query.query)
                            or not -5 <= (when - result.evaluated_at).total_seconds() <= 30
                            or len(result.candidates) > intent.query.query.limit
                            or any(item.expires_at <= when or item.listing.profile != intent.query.query.profile for item in result.candidates)):
                        raise ValueError("network search reply does not match its intent")
                elif not isinstance(result, PeerResponse):
                    raise ValueError("network peer reply has the wrong contract")
                else:
                    try:
                        checked_reply(intent.query.request, result.model_dump(mode="json"))
                    except TransportError:
                        raise ValueError("network peer reply does not match its intent") from None
                encoded = canonical_json(result)
                if len(encoded) > MAX_NETWORK_RESULT:
                    result, error = None, NetworkError(code="network_response_too_large", status=502)
            db.execute("UPDATE network_jobs SET state=?,finished=?,result=?,error=?,claim=NULL,dispatcher=NULL WHERE id=?",
                ("succeeded" if result is not None else "failed", when.timestamp(),
                 canonical_json(result) if result is not None else None,
                 canonical_json(error) if error is not None else None, completion.job_id))
            return {"accepted": True}
