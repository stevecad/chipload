"""Authorized execution admission and durable operation records, without adapter IO."""

from datetime import timedelta

from .access import Principal, require_operation
from .artifact import canonical_json, digest
from .evaluate import EvaluationRequest, evaluate
from .execution_model import (
    MAX_EXECUTION_BYTES,
    RESULT_RESERVATION_BYTES,
    AdapterBinding,
    AdapterResult,
    ExecutionRecord,
    ExecutionRequest,
)
from .lifecycle import Conflict, IntegrityError, NotFound, Repository, now
from .model import UNITS, Fact, OperationField, Quantity, ShopOperation
from .project import unique_json
from .published import PublishedService
from .quotas import QuotaExceeded, scopes


def validate_values(fields: tuple[OperationField, ...], values: tuple[Fact, ...]) -> None:
    declared = {field.name: field for field in fields}
    if len({value.name for value in values}) != len(values):
        raise ValueError("operation values must have unique names")
    present = {value.name for value in values}
    if present - declared.keys() or any(f.required and f.name not in present for f in fields):
        raise ValueError("operation values do not match the declared fields")
    for fact in values:
        field, value = declared[fact.name], fact.value
        valid = (
            (field.kind == "text" and type(value) is str)
            or (field.kind == "boolean" and type(value) is bool)
            or (
                field.kind == "quantity"
                and isinstance(value, Quantity)
                and value.dimension == UNITS[field.unit].dimension
            )
        )
        if not valid:
            raise ValueError("operation value has an incompatible type or unit")


def published(repository, db, shop):
    row = db.execute("SELECT * FROM shops WHERE id=?", (shop,)).fetchone()
    if row is None or row["published_build"] is None:
        raise NotFound("published shop not found")
    build = repository.build_record(
        db.execute(
            "SELECT * FROM builds WHERE id=? AND shop_id=?",
            (row["published_build"], shop),
        ).fetchone()
    )
    if build.state != "succeeded" or build.report is None or build.report.artifact is None:
        raise IntegrityError("invalid operation publication")
    return row["generation"], build, build.report.artifact


def operation_from(artifact, operation_id) -> ShopOperation:
    operation = next((item for item in artifact.shop.operations if item.id == operation_id), None)
    if operation is None:
        raise NotFound("operation not found")
    return operation


def checks_pass(db, artifact, operation, request, at):
    resources = {r.id: r for r in artifact.shop.resources}
    if any(resources[name].status != "active" for name in operation.resources):
        return False
    observed = PublishedService._observations(
        db.execute(
            "SELECT * FROM observation_heads WHERE shop_id=?",
            (request.shop_id,),
        ).fetchone()
    )
    inputs = {fact.name: fact for fact in request.inputs}
    for check in operation.checks:
        result = evaluate(
            artifact,
            EvaluationRequest(
                target_kind=check.target_kind,
                target=check.target,
                facts=tuple(inputs[name] for name in check.inputs if name in inputs),
                initial_state=check.initial_state,
                purpose="readiness",
                minimum_evidence=check.minimum_evidence,
                allow_simulated=request.mode == "simulated",
            ),
            at=at,
            observations=observed.observations,
        )
        if result.outcome != "supported":
            return False
    return True


def record(row) -> ExecutionRecord:
    if row is None:
        raise NotFound("execution not found")
    try:
        binding = AdapterBinding.model_validate(unique_json(bytes(row["binding"])))
        result = (
            AdapterResult.model_validate(unique_json(bytes(row["result"])))
            if row["result"]
            else None
        )
        return ExecutionRecord(
            id=row["id"],
            shop_id=row["shop_id"],
            operation_id=row["operation_id"],
            actor=row["actor"],
            mode=binding.mode,
            publication_generation=row["publication_generation"],
            model_sha256=row["model_sha256"],
            binding_generation=row["binding_generation"],
            state=row["state"],
            created_at=row["created_at"],
            deadline=row["deadline"],
            finished_at=row["finished_at"],
            cancel_requested=bool(row["cancel_requested"]),
            result=result,
        )
    except (ValueError, KeyError, TypeError):
        raise IntegrityError("invalid stored execution") from None


def finish(repository, db, row, result, at, actor):
    terminal = result.state != "indeterminate"
    db.execute(
        "UPDATE executions SET state=?,result=?,finished_at=?,claim=NULL WHERE id=?",
        (result.state, canonical_json(result), at.isoformat() if terminal else None, row["id"]),
    )
    if terminal:
        db.execute("DELETE FROM execution_resources WHERE execution_id=?", (row["id"],))
    repository.event(db, row["shop_id"], actor, "operation." + result.state, row["id"])


def admit(db, policy, shop, actor, size, at, *, running=False):
    for scope, limit, where, params in scopes(policy, shop, actor):
        counts = db.execute(
            "SELECT COUNT(*) AS retained, COALESCE(SUM(state IN ('queued','running','indeterminate')),0) AS pending, "
            "COALESCE(SUM(state IN ('running','indeterminate')),0) AS running, COALESCE(SUM(created_at>?),0) AS recent, "
            "COALESCE(SUM(length(request)+length(binding)+?),0) AS bytes FROM executions " + where,
            ((at - timedelta(hours=1)).isoformat(), RESULT_RESERVATION_BYTES, *params),
        ).fetchone()
        if running:
            if counts["running"] >= limit.running_operations:
                raise QuotaExceeded(f"{scope}.running_operations", retry_after=5)
            continue
        for key, label, maximum, retry in (
            ("pending", "pending_operations", limit.pending_operations, 5),
            ("recent", "operations_per_hour", limit.operations_per_hour, 3600),
            ("retained", "retained_operations", limit.retained_operations, None),
        ):
            if counts[key] >= maximum:
                raise QuotaExceeded(f"{scope}.{label}", retry_after=retry)
        if counts["bytes"] + size + RESULT_RESERVATION_BYTES > limit.operation_bytes:
            raise QuotaExceeded(f"{scope}.operation_bytes")


class ExecutionService:
    def __init__(self, repository: Repository, *, clock=now):
        self.repository, self.clock = repository, clock

    def inspect_operation(self, principal, shop, operation_id):
        require_operation(principal, shop, operation_id, "operation:read")
        with self.repository.connection() as db:
            db.execute("BEGIN")
            generation, _, artifact = published(self.repository, db, shop)
            operation = operation_from(artifact, operation_id)
            binding = db.execute(
                "SELECT * FROM operation_bindings WHERE shop_id=? AND operation_id=?",
                (shop, operation_id),
            ).fetchone()
            current = AdapterBinding.model_validate_json(binding["binding"]) if binding else None
            return {
                "generation": generation,
                "model_sha256": artifact.model_sha256,
                "operation": operation.model_dump(mode="json"),
                "binding_generation": binding["generation"] if binding else 0,
                "enabled": bool(
                    current and current.enabled and current.model_sha256 == artifact.model_sha256
                ),
                "mode": current.mode if current else None,
            }

    def bind(self, binding: AdapterBinding, *, expected_generation: int):
        """Trusted local operator method. Never register as a shop HTTP/MCP action."""
        binding = AdapterBinding.model_validate(binding.model_dump(mode="python"))
        with self.repository.transaction() as db:
            _, _, artifact = published(self.repository, db, binding.shop_id)
            operation_from(artifact, binding.operation_id)
            if artifact.model_sha256 != binding.model_sha256:
                raise Conflict("binding must name the current published model")
            row = db.execute(
                "SELECT * FROM operation_bindings WHERE shop_id=? AND operation_id=?",
                (binding.shop_id, binding.operation_id),
            ).fetchone()
            generation = row["generation"] if row else 0
            if generation != expected_generation:
                raise Conflict("operation binding changed")
            db.execute(
                "INSERT INTO operation_bindings VALUES(?,?,?,?) ON CONFLICT(shop_id,operation_id) "
                "DO UPDATE SET generation=excluded.generation,binding=excluded.binding",
                (binding.shop_id, binding.operation_id, generation + 1, canonical_json(binding)),
            )
            self.repository.event(
                db, binding.shop_id, "local-operator", "operation.bound", binding.operation_id
            )
            return {"generation": generation + 1, "binding": binding.model_dump(mode="json")}

    def submit(self, principal: Principal, request: ExecutionRequest):
        request = ExecutionRequest.model_validate(request.model_dump(mode="python"))
        require_operation(principal, request.shop_id, request.operation_id, "operation:execute")
        data = canonical_json(request)
        if len(data) > MAX_EXECUTION_BYTES:
            raise ValueError("execution request exceeds its byte limit")
        request_hash, at = digest(data), self.clock()
        with self.repository.transaction() as db:
            previous = db.execute(
                "SELECT * FROM executions WHERE shop_id=? AND actor=? AND request_key=?",
                (request.shop_id, principal.subject, request.request_key),
            ).fetchone()
            if previous:
                if previous["request_sha256"] != request_hash:
                    raise Conflict("request key already identifies another execution request")
                return record(previous)
            generation, build, artifact = published(self.repository, db, request.shop_id)
            if generation != request.expected_generation:
                raise Conflict("publication changed before execution admission")
            operation = operation_from(artifact, request.operation_id)
            row = db.execute(
                "SELECT * FROM operation_bindings WHERE shop_id=? AND operation_id=?",
                (request.shop_id, request.operation_id),
            ).fetchone()
            if row is None:
                raise Conflict("operation has no enabled adapter binding")
            binding = AdapterBinding.model_validate_json(row["binding"])
            if (
                not binding.enabled
                or binding.model_sha256 != artifact.model_sha256
                or binding.mode != request.mode
            ):
                raise Conflict("operation binding does not authorize this model and execution mode")
            validate_values(operation.inputs, request.inputs)
            if not checks_pass(db, artifact, operation, request, at):
                raise Conflict(
                    "operation preconditions are not supported by current evidence and observations"
                )
            admit(
                db,
                self.repository.policy(),
                request.shop_id,
                principal.subject,
                len(data) + len(row["binding"]),
                at,
            )
            identifier = digest(
                canonical_json(
                    {
                        "repository": self.repository.admission.identity,
                        "shop": request.shop_id,
                        "actor": principal.subject,
                        "request_key": request.request_key,
                    }
                )
            )
            db.execute(
                "INSERT INTO executions(id,shop_id,operation_id,actor,request_key,request_sha256,request,"
                "build_id,model_sha256,publication_generation,binding_generation,binding,state,created_at,deadline) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?)",
                (
                    identifier,
                    request.shop_id,
                    request.operation_id,
                    principal.subject,
                    request.request_key,
                    request_hash,
                    data,
                    build.id,
                    artifact.model_sha256,
                    generation,
                    row["generation"],
                    row["binding"],
                    at.isoformat(),
                    (at + timedelta(minutes=5)).isoformat(),
                ),
            )
            self.repository.event(
                db, request.shop_id, principal.subject, "operation.requested", identifier
            )
            return record(
                db.execute("SELECT * FROM executions WHERE id=?", (identifier,)).fetchone()
            )

    def read(self, principal, shop, operation_id, execution_id):
        require_operation(principal, shop, operation_id, "operation:read")
        with self.repository.connection() as db:
            return record(
                db.execute(
                    "SELECT * FROM executions WHERE id=? AND shop_id=? AND operation_id=?",
                    (execution_id, shop, operation_id),
                ).fetchone()
            )

    def cancel(self, principal, shop, operation_id, execution_id):
        require_operation(principal, shop, operation_id, "operation:cancel")
        with self.repository.transaction() as db:
            row = db.execute(
                "SELECT * FROM executions WHERE id=? AND shop_id=? AND operation_id=?",
                (execution_id, shop, operation_id),
            ).fetchone()
            current = record(row)
            if current.state in {"succeeded", "rejected", "cancelled"}:
                return current
            if not current.cancel_requested:
                db.execute("UPDATE executions SET cancel_requested=1 WHERE id=?", (execution_id,))
                self.repository.event(
                    db, shop, principal.subject, "operation.cancel-requested", execution_id
                )
            if current.state == "queued":
                finish(
                    self.repository,
                    db,
                    row,
                    AdapterResult(state="cancelled", code="cancelled-before-dispatch"),
                    self.clock(),
                    principal.subject,
                )
            return record(
                db.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
            )
