"""Trusted Python runner: dispatch each SDK call with current scoped grants."""

import logging
import threading
import time
import uuid
from datetime import timedelta

from .access import Forbidden, Principal, require
from .artifact import canonical_json, digest
from .launches import LaunchGuard, LaunchUncertain
from .lifecycle import Conflict, ShopService, now
from .operations import Operations
from .program_model import MAX_CALLS, MAX_PROGRAM_RESULT, PROGRAM_RESERVATION
from .programs import decode, intersect, request_for
from .project import unique_json
from .quotas import scopes
from .sandbox import SandboxError
from .service_errors import problem


def refusal(error):
    status, detail = problem(error)
    return {"ok": False, "status": status, "detail": detail}


class ProgramProcessor:
    def __init__(
        self, repository, runner, principal_for, *, stopping=None, clock=now, operations=None,
        assessment_runner=None,
    ):
        self.repository, self.runner, self.principal_for = repository, runner, principal_for
        self.stopping, self.clock = stopping or threading.Event(), clock
        self.contained_service = operations is None
        self.operations = operations or Operations(ShopService(repository))
        self.assessment_runner = assessment_runner

    def claim_next(self):
        with self.repository.transaction() as db:
            at = self.clock()
            db.execute(
                "UPDATE python_runs SET state='interrupted',finished_at=?,result=? WHERE id IN (SELECT id FROM python_runs WHERE state='queued' AND deadline<=? LIMIT 100)",
                (
                    at.isoformat(),
                    canonical_json({"error": "Python queue deadline expired"}),
                    at.isoformat(),
                ),
            )
            rows = db.execute(
                "SELECT * FROM python_runs WHERE state='queued' ORDER BY scan_turn,created_at,id LIMIT 100"
            ).fetchall()
            policy = self.repository.policy()
            turn = db.execute("SELECT COALESCE(MAX(scan_turn),0)+1 FROM python_runs").fetchone()[0]
            for row in rows:
                db.execute("UPDATE python_runs SET scan_turn=? WHERE id=?", (turn, row["id"]))
                decode(row)
                available = True
                for _, limit, where, values in scopes(policy, row["shop_id"], row["actor"]):
                    clause = where + " AND " if where else "WHERE "
                    count = db.execute(
                        f"SELECT COUNT(*) FROM python_runs {clause}state='running'", values
                    ).fetchone()[0]
                    if count >= limit.running_programs:
                        available = False
                        break
                if not available:
                    continue
                request = request_for(row)
                claim = uuid.uuid4().hex
                deadline = (at + timedelta(seconds=request.seconds + 60)).isoformat()
                db.execute(
                    "UPDATE python_runs SET state='running',claim=?,deadline=? WHERE id=?",
                    (claim, deadline, row["id"]),
                )
                event = "assessment" if row["kind"] == "assessment" else "code"
                self.repository.event(db, row["shop_id"], row["actor"], event + ".started", row["id"])
                return dict(row) | {
                    "state": "running",
                    "claim": claim,
                    "deadline": deadline,
                }, request
        return None

    def active(self, identifier, claim):
        if self.stopping.is_set():
            return False
        with self.repository.connection(timeout=0.1) as db:
            row = db.execute(
                "SELECT state,claim,deadline,cancel_requested FROM python_runs WHERE id=?",
                (identifier,),
            ).fetchone()
        return bool(
            row
            and row["state"] == "running"
            and row["claim"] == claim
            and row["deadline"] > self.clock().isoformat()
            and not row["cancel_requested"]
        )

    def principal(self, row):
        ceiling = Principal.model_validate(unique_json(row["ceiling"]))
        if ceiling.subject != row["actor"] or any(
            g.shop_id != row["shop_id"] for g in ceiling.grants
        ):
            raise Forbidden("invalid Python run scope")
        who = intersect(ceiling, self.principal_for(row["actor"]))
        if row["kind"] == "assessment":
            from .assessments import AssessmentService

            return AssessmentService.authorize(who, request_for(row))
        return require(who, row["shop_id"], "code:run")

    def dispatch(self, row, operation, request, *, deadline=None):
        if not self.active(row["id"], row["claim"]):
            raise SandboxError("Python run is no longer active")
        if (
            not isinstance(operation, str)
            or operation not in self.operations.specs
            or operation.startswith("code_")
            or operation in {"network_claim", "network_complete"}
        ):
            raise SandboxError("unsupported SDK operation")
        if not isinstance(request, dict):
            raise SandboxError("invalid SDK arguments")
        # Validate arguments and permissions outside the source process, for every call.
        who = self.principal(row)
        spec = self.operations.specs[operation]
        entry = {
            "operation": operation,
            "request_sha256": digest(canonical_json(request)),
            "state": "started",
            "read_only": spec.read_only,
        }
        with self.repository.transaction() as db:
            current = db.execute("SELECT * FROM python_runs WHERE id=?", (row["id"],)).fetchone()
            if (
                current["state"] != "running"
                or current["claim"] != row["claim"]
                or current["cancel_requested"]
                or current["deadline"] <= self.clock().isoformat()
            ):
                raise SandboxError("Python run is no longer active")
            calls = unique_json(current["calls"])
            if len(calls) >= MAX_CALLS:
                raise SandboxError("Python run exceeded its SDK call limit")
            ordinal = len(calls)
            calls.append(entry)
            db.execute(
                "UPDATE python_runs SET calls=? WHERE id=?", (canonical_json(calls), row["id"])
            )
        try:
            self.operations.limiter.identity(who.subject)
            if self.contained_service:
                value = self.runner.service_call(
                    row["id"],
                    operation,
                    who,
                    request,
                    deadline=deadline if deadline is not None else time.monotonic() + 5,
                    keep_running=lambda: self.active(row["id"], row["claim"]),
                )
            else:
                # Trusted test/service injection; the packaged broker always
                # uses the contained service supplied by DockerProgramRunner.
                value = self.operations.call(operation, who, request)
            answer = {"ok": True, "value": value}
            entry["state"] = "returned"
            if isinstance(value, dict):
                entry["references"] = {
                    key: value[key]
                    for key in ("id", "revision", "build_id", "generation", "state")
                    if isinstance(value.get(key), (str, int)) and len(str(value[key])) <= 100
                }
        except SandboxError:
            raise  # Preserve the started journal entry for an ambiguous call.
        except Exception as error:
            answer = refusal(error)
            entry["state"], entry["status"] = "error", answer["status"]
        # A crash between the service call and this commit leaves an honest started entry.
        with self.repository.transaction() as db:
            current = db.execute("SELECT * FROM python_runs WHERE id=?", (row["id"],)).fetchone()
            if current["state"] != "running" or current["claim"] != row["claim"]:
                raise SandboxError("Python completion was fenced")
            calls = unique_json(current["calls"])
            calls[ordinal] = entry
            db.execute(
                "UPDATE python_runs SET calls=? WHERE id=?", (canonical_json(calls), row["id"])
            )
        return answer

    def finish(self, row, result, state):
        payload = canonical_json(result)
        if len(canonical_json(result.get("value"))) > MAX_PROGRAM_RESULT:
            raise ValueError("Python result exceeds its byte limit")
        with self.repository.transaction() as db:
            current = db.execute("SELECT * FROM python_runs WHERE id=?", (row["id"],)).fetchone()
            if current["state"] != "running" or current["claim"] != row["claim"]:
                raise Conflict("Python run claim is no longer current")
            if current["deadline"] <= self.clock().isoformat():
                raise Conflict("Python run claim has expired")
            if db.execute(
                "SELECT 1 FROM worker_launches WHERE kind='python_runs' AND job_id=? AND settled=0",
                (row["id"],),
            ).fetchone():
                raise Conflict("container creation is unresolved")
            if (
                len(payload)
                + len(current["request"])
                + len(current["ceiling"])
                + len(current["calls"])
                > PROGRAM_RESERVATION
            ):
                raise ValueError("Python run exceeds its storage reservation")
            if current["cancel_requested"]:
                state = "cancelled"
            db.execute(
                "UPDATE python_runs SET state=?,result=?,finished_at=?,claim=NULL WHERE id=?",
                (state, payload, self.clock().isoformat(), row["id"]),
            )
            event = "assessment" if row["kind"] == "assessment" else "code"
            self.repository.event(db, row["shop_id"], row["actor"], event + "." + state, row["id"])

    def run_next(self):
        selected = self.claim_next()
        if selected is None:
            return False
        row, request = selected
        guard = LaunchGuard(self.repository, "python_runs", row["id"], row["claim"], clock=self.clock)
        try:
            self.principal(row)
            if row["kind"] == "assessment":
                from .assessment_runner import AssessmentProcessor, DockerAssessmentRunner

                runner = self.assessment_runner or DockerAssessmentRunner(self.runner.environment)
                result = AssessmentProcessor(self.repository, runner, clock=self.clock).execute(
                    row, request, guard=guard,
                    keep_running=lambda: self.active(row["id"], row["claim"]),
                )
                self.principal(row)
            else:
                result = self.runner.run(
                    row["id"],
                    request,
                    lambda operation, body, *, deadline=None: self.dispatch(
                        row, operation, body, deadline=deadline
                    ),
                    lambda: self.active(row["id"], row["claim"]),
                    guard=guard,
                )
            state = "failed" if "error" in result else "succeeded"
        except Forbidden:
            result, state = {"error": "Python execution grant is unavailable"}, "failed"
        except Exception:
            # Cleanup must be confirmed before releasing capacity or recording termination.
            self.runner.remove(row["id"], guard=guard)
            result, state = (
                {"error": "Python run interrupted; inspect recorded calls before further changes"},
                "interrupted",
            )
        try:
            self.finish(row, result, state)
        except Conflict:
            pass
        return True

    def recover_expired(self):
        with self.repository.connection() as db:
            expired = db.execute(
                "SELECT * FROM python_runs WHERE state='running' AND deadline<=? LIMIT 100",
                (self.clock().isoformat(),),
            ).fetchall()
        count = 0
        for old in expired:
            with self.repository.transaction() as db:
                claim = uuid.uuid4().hex
                changed = db.execute(
                    "UPDATE python_runs SET claim=?,deadline=? WHERE id=? AND state='running' AND claim=? AND deadline<=?",
                    (
                        claim,
                        (self.clock() + timedelta(seconds=30)).isoformat(),
                        old["id"],
                        old["claim"],
                        self.clock().isoformat(),
                    ),
                )
                if changed.rowcount != 1:
                    continue
            guard = LaunchGuard(self.repository, "python_runs", old["id"], claim, clock=self.clock)
            try:
                self.runner.remove(old["id"], guard=guard)
            except LaunchUncertain:
                logging.getLogger(__name__).warning("Python creation remains unresolved: %s", old["id"])
                continue
            row = dict(old) | {"claim": claim}
            self.finish(
                row,
                {"error": "Python worker was interrupted; recorded calls may have committed"},
                "interrupted",
            )
            count += 1
        return count
