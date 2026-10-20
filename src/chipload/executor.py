"""Trusted adapter execution and reconciliation; never imports shop source."""

import argparse
import json
import os
import stat
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from pydantic import Field, model_validator

from .access import Forbidden, require_operation
from .artifact import canonical_json, digest
from .execution import (
    admit,
    checks_pass,
    finish,
    operation_from,
    published,
    validate_values,
)
from .execution_model import MAX_EXECUTION_BYTES, AdapterBinding, AdapterResult, ExecutionRequest
from .lifecycle import Conflict, IntegrityError, Repository, now
from .model import Fact, Id, Model, ShopOperation
from .project import unique_json
from .quotas import QuotaExceeded


@dataclass(frozen=True)
class ExecutionContext:
    execution_id: str
    intent_sha256: str
    claim: str | None
    request: ExecutionRequest
    operation: ShopOperation


class Adapter(Protocol):
    mode: str

    def execute(self, context: ExecutionContext) -> AdapterResult: ...

    def reconcile(self, context: ExecutionContext) -> AdapterResult | None: ...


class SimulatedAdapter:
    """An explicit simulated effect ledger. No equipment or network access occurs."""

    mode = "simulated"

    def __init__(self, repository: Repository, outputs: tuple[Fact, ...], *, clock=now):
        self.repository, self.outputs = repository, outputs
        self.clock = clock

    @staticmethod
    def _existing(db, context):
        row = db.execute(
            "SELECT * FROM simulation_effects WHERE execution_id=?", (context.execution_id,)
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != context.intent_sha256:
            return AdapterResult(state="indeterminate", code="adapter-request-conflict")
        return AdapterResult.model_validate(unique_json(bytes(row["result"])))

    def execute(self, context):
        with self.repository.transaction() as db:
            existing = self._existing(db, context)
            if existing is not None:
                return existing
            row = db.execute(
                "SELECT * FROM executions WHERE id=?", (context.execution_id,)
            ).fetchone()
            if (
                row is None
                or row["state"] != "running"
                or row["claim"] != context.claim
                or row["deadline"] <= self.clock().isoformat()
            ):
                return AdapterResult(state="rejected", code="dispatch-fenced")
            if row["cancel_requested"]:
                result = AdapterResult(state="cancelled", code="adapter-cancelled-before-effect")
            else:
                validate_values(context.operation.outputs, self.outputs)
                result = AdapterResult(
                    state="succeeded", code="simulated-effect", outputs=self.outputs
                )
            data = canonical_json(result)
            if len(data) > MAX_EXECUTION_BYTES:
                raise ValueError("simulation output exceeds its bound")
            db.execute(
                "INSERT INTO simulation_effects VALUES(?,?,?)",
                (context.execution_id, context.intent_sha256, data),
            )
            return result

    def reconcile(self, context):
        with self.repository.transaction() as db:
            existing = self._existing(db, context)
            if existing is not None:
                return existing
            row = db.execute(
                "SELECT state,claim FROM executions WHERE id=?", (context.execution_id,)
            ).fetchone()
            # The same writer lock and revoked claim prevent a late simulated effect.
            if row and row["state"] == "indeterminate" and row["claim"] is None:
                return AdapterResult(state="rejected", code="simulation-confirmed-no-effect")
            return None


class OperationRunner:
    def __init__(
        self, repository: Repository, adapters: dict[str, Adapter], principal_for, *, clock=now
    ):
        self.repository, self.adapters = repository, adapters
        self.principal_for, self.clock = principal_for, clock

    def _context(self, row):
        request = ExecutionRequest.model_validate(unique_json(bytes(row["request"])))
        binding = AdapterBinding.model_validate(unique_json(bytes(row["binding"])))
        if (
            digest(bytes(row["request"])) != row["request_sha256"]
            or request.shop_id != row["shop_id"]
            or request.operation_id != row["operation_id"]
            or request.expected_generation != row["publication_generation"]
            or request.request_key != row["request_key"]
            or request.mode != binding.mode
            or binding.shop_id != row["shop_id"]
            or binding.operation_id != row["operation_id"]
            or binding.model_sha256 != row["model_sha256"]
        ):
            raise IntegrityError("execution request integrity check failed")
        with self.repository.connection() as db:
            build = self.repository.build_record(
                db.execute(
                    "SELECT * FROM builds WHERE id=? AND shop_id=?",
                    (row["build_id"], row["shop_id"]),
                ).fetchone()
            )
        if (
            build.state != "succeeded"
            or not build.report
            or not build.report.artifact
            or build.report.artifact.model_sha256 != row["model_sha256"]
        ):
            raise IntegrityError("execution model integrity check failed")
        operation = operation_from(build.report.artifact, row["operation_id"])
        validate_values(operation.inputs, request.inputs)
        intent = digest(
            canonical_json(
                {
                    "request": row["request_sha256"],
                    "model": row["model_sha256"],
                    "binding": digest(bytes(row["binding"])),
                }
            )
        )
        return ExecutionContext(row["id"], intent, row["claim"], request, operation)

    def expire(self):
        at = self.clock()
        with self.repository.transaction() as db:
            rows = db.execute(
                "SELECT * FROM executions WHERE state IN ('queued','running') AND deadline<=? LIMIT 100",
                (at.isoformat(),),
            ).fetchall()
            for row in rows:
                result = AdapterResult(
                    state="rejected" if row["state"] == "queued" else "indeterminate",
                    code="queue-expired" if row["state"] == "queued" else "dispatch-expired",
                )
                finish(self.repository, db, row, result, at, "executor")
            return len(rows)

    def claim_next(self):
        at = self.clock()
        with self.repository.transaction() as db:
            policy = self.repository.policy()
            candidates = db.execute(
                "SELECT e.* FROM executions e "
                "LEFT JOIN execution_turns s ON s.scope='shop' AND s.subject=e.shop_id "
                "LEFT JOIN execution_turns a ON a.scope='identity' AND a.subject=e.actor "
                "WHERE e.state='queued' ORDER BY e.scan_turn,COALESCE(s.turn,0),COALESCE(a.turn,0),e.sequence LIMIT 100"
            ).fetchall()
            scan_turn = db.execute(
                "SELECT COALESCE(MAX(scan_turn),0)+1 FROM executions"
            ).fetchone()[0]
            for row in candidates:
                # Rotate bounded scans past blocked or unregistered adapter jobs.
                db.execute("UPDATE executions SET scan_turn=? WHERE id=?", (scan_turn, row["id"]))
                binding = AdapterBinding.model_validate(unique_json(bytes(row["binding"])))
                adapter = self.adapters.get(binding.adapter_id)
                if adapter is None:
                    continue
                request = self._context(row).request
                current = db.execute(
                    "SELECT * FROM operation_bindings WHERE shop_id=? AND operation_id=?",
                    (row["shop_id"], row["operation_id"]),
                ).fetchone()
                reason = None
                try:
                    require_operation(
                        self.principal_for(row["actor"]),
                        row["shop_id"],
                        row["operation_id"],
                        "operation:execute",
                    )
                except Forbidden:
                    reason = "execution-grant-revoked"
                if (
                    current is None
                    or current["generation"] != row["binding_generation"]
                    or current["binding"] != row["binding"]
                    or not binding.enabled
                ):
                    reason = "adapter-binding-changed"
                if adapter.mode != binding.mode:
                    reason = "adapter-mode-mismatch"
                shop = db.execute("SELECT * FROM shops WHERE id=?", (row["shop_id"],)).fetchone()
                if (
                    not shop
                    or shop["generation"] != row["publication_generation"]
                    or shop["published_build"] != row["build_id"]
                ):
                    reason = "operation-publication-changed"
                if row["deadline"] <= at.isoformat():
                    reason = "queue-expired"
                if reason:
                    finish(
                        self.repository,
                        db,
                        row,
                        AdapterResult(state="rejected", code=reason),
                        at,
                        "executor",
                    )
                    continue
                _, _, artifact = published(self.repository, db, row["shop_id"])
                operation = operation_from(artifact, row["operation_id"])
                validate_values(operation.inputs, request.inputs)
                if not checks_pass(db, artifact, operation, request, at):
                    finish(
                        self.repository,
                        db,
                        row,
                        AdapterResult(state="rejected", code="operation-precondition-changed"),
                        at,
                        "executor",
                    )
                    continue
                try:
                    admit(db, policy, row["shop_id"], row["actor"], 0, at, running=True)
                except QuotaExceeded:
                    continue
                if any(
                    db.execute(
                        "SELECT 1 FROM execution_resources WHERE shop_id=? AND resource_id=?",
                        (row["shop_id"], name),
                    ).fetchone()
                    for name in operation.resources
                ):
                    continue
                claim = uuid.uuid4().hex
                db.execute(
                    "UPDATE executions SET state='running',claim=?,deadline=? WHERE id=?",
                    (
                        claim,
                        (at + timedelta(seconds=operation.timeout_seconds)).isoformat(),
                        row["id"],
                    ),
                )
                for resource in operation.resources:
                    db.execute(
                        "INSERT INTO execution_resources VALUES(?,?,?)",
                        (row["shop_id"], resource, row["id"]),
                    )
                turn = db.execute("SELECT COALESCE(MAX(turn),0)+1 FROM execution_turns").fetchone()[
                    0
                ]
                for scope, subject in (("shop", row["shop_id"]), ("identity", row["actor"])):
                    db.execute(
                        "INSERT INTO execution_turns VALUES(?,?,?) ON CONFLICT(scope,subject) DO UPDATE SET turn=excluded.turn",
                        (scope, subject, turn),
                    )
                self.repository.event(
                    db, row["shop_id"], "executor", "operation.dispatched", row["id"]
                )
                return db.execute("SELECT * FROM executions WHERE id=?", (row["id"],)).fetchone()
        return None

    def complete(self, context, result, *, reconciliation=False):
        result = AdapterResult.model_validate(result.model_dump(mode="python"))
        if result.state == "succeeded":
            validate_values(context.operation.outputs, result.outputs)
        if len(canonical_json(result)) > MAX_EXECUTION_BYTES:
            raise ValueError("adapter result exceeds its byte limit")
        at = self.clock()
        with self.repository.transaction() as db:
            row = db.execute(
                "SELECT * FROM executions WHERE id=?", (context.execution_id,)
            ).fetchone()
            if reconciliation:
                valid = row and row["state"] == "indeterminate" and row["claim"] is None
            else:
                valid = row and row["state"] == "running" and row["claim"] == context.claim
            if not valid:
                raise Conflict("execution claim no longer owns completion")
            if not reconciliation and row["deadline"] <= at.isoformat():
                result = AdapterResult(state="indeterminate", code="dispatch-expired")
            finish(self.repository, db, row, result, at, "executor")

    def run_next(self):
        self.expire()
        row = self.claim_next()
        if row is None:
            return False
        context = self._context(row)
        binding = AdapterBinding.model_validate_json(row["binding"])
        try:
            result = self.adapters[binding.adapter_id].execute(context)
        except Exception:
            # Adapter exceptions and invalid output can follow an accepted effect.
            # Never expose raw adapter exception text, which may include secrets.
            result = AdapterResult(state="indeterminate", code="adapter-outcome-unknown")
        try:
            self.complete(context, result)
        except Conflict:
            pass  # A fenced caller cannot overwrite a later reconciliation.
        except (ValueError, TypeError, AttributeError):
            try:
                self.complete(
                    context, AdapterResult(state="indeterminate", code="adapter-output-invalid")
                )
            except Conflict:
                pass
        return True

    def reconcile(self, execution_id):
        self.expire()
        with self.repository.connection() as db:
            row = db.execute(
                "SELECT * FROM executions WHERE id=? AND state='indeterminate'", (execution_id,)
            ).fetchone()
        if row is None:
            return False
        binding = AdapterBinding.model_validate_json(row["binding"])
        adapter = self.adapters.get(binding.adapter_id)
        if adapter is None or adapter.mode != binding.mode:
            return False
        context = self._context(row)
        try:
            result = adapter.reconcile(context)
            if result is not None and result.state != "indeterminate":
                self.complete(context, result, reconciliation=True)
                return True
        except Exception:
            pass
        return False

    def reconcile_pending(self):
        self.expire()
        with self.repository.transaction() as db:
            rows = db.execute(
                "SELECT id FROM executions WHERE state='indeterminate' "
                "ORDER BY scan_turn,sequence LIMIT 100"
            ).fetchall()
            turn = db.execute("SELECT COALESCE(MAX(scan_turn),0)+1 FROM executions").fetchone()[0]
            db.executemany(
                "UPDATE executions SET scan_turn=? WHERE id=?", ((turn, row["id"]) for row in rows)
            )
        # Adapter queries occur outside the writer transaction. Completion still
        # checks the durable state, so competing reconcilers cannot overwrite it.
        return sum(self.reconcile(row["id"]) for row in rows)


def private_json(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("executor configuration must be an owned private regular file")
        data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("executor configuration exceeds its byte limit")
        return unique_json(data)


class Simulator(Model):
    id: Id
    outputs: tuple[Fact, ...] = Field(max_length=100)


class Simulators(Model):
    adapters: tuple[Simulator, ...] = Field(max_length=100)

    @model_validator(mode="after")
    def unique(self):
        if len({entry.id for entry in self.adapters}) != len(self.adapters):
            raise ValueError("simulator ids must be unique")
        for entry in self.adapters:
            result = AdapterResult(
                state="succeeded", code="simulated-effect", outputs=entry.outputs
            )
            if len(canonical_json(result)) > MAX_EXECUTION_BYTES:
                raise ValueError("simulator outputs exceed their byte limit")
        return self


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--simulators", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    # Network/real adapters are intentionally not loaded from shop projects.
    from .auth import ServerSettings

    settings = ServerSettings.model_validate(private_json(args.settings))
    repository = Repository(settings.database)
    configuration = Simulators.model_validate(private_json(args.simulators))
    adapters = {
        entry.id: SimulatedAdapter(repository, entry.outputs) for entry in configuration.adapters
    }
    while True:
        # Reload explicit grants between claims; token role ceilings were checked on admission.
        current = ServerSettings.model_validate(private_json(args.settings))
        if current.database != settings.database:
            raise ValueError("executor storage changed; restart with the intended settings")
        principals = {who.subject: who for who in current.bindings}
        runner = OperationRunner(repository, adapters, principals.get)
        ran = runner.run_next()
        reconciled = runner.reconcile_pending()
        if args.once:
            print(json.dumps({"dispatched": ran, "reconciled": reconciled}))
            break
        if not ran:
            time.sleep(0.5)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(
            "executor failed; inspect private configuration and repository state"
        ) from None
