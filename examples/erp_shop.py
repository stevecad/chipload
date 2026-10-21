"""Shop code that reads external stock facts; the ERP owns its inventory records."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from chipload import (
    BuildIdentity,
    Capability,
    Choice,
    Evidence,
    ExternalReference,
    Fact,
    Observation,
    ObservationRequirement,
    OperationCheck,
    OperationField,
    Process,
    Quantity,
    Range,
    Resource,
    Shop,
    ShopOperation,
    compile_shop,
    quantity,
)
from chipload.client import ServiceError
from chipload.evaluate import EvaluationRequest
from chipload.execution_model import ExecutionRecord
from chipload.project import AcceptanceCase, SourceFile, SourceProject, run_cases
from chipload.published import ObservationSnapshot

AT = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
SHOP = "erp-connected-example"
SYSTEM = "example-erp"
STOCK_RECORD = "material-stock-demo"
READ_OPERATION = "read-material-stock"
OBSERVATION = "stock-for-one-unit"
MAX_AGE = 60


def define_shop():
    business = (
        Choice(fact="engagement", allowed=("prototype",)),
        Range(fact="quantity", minimum=quantity("1", "1"), maximum=quantity("1", "1")),
    )
    access = (
        Choice(fact="record-reference", allowed=(STOCK_RECORD,)),
        Choice(fact="purpose", allowed=("shop-readiness",)),
    )
    return Shop(
        id=SHOP, name="ERP-connected shop example", visibility="public",
        resources=(Resource(id="material-bin", name="Material supply", kind="stock-location"),),
        processes=(Process(id="provide-service", name="Example service", visibility="public"),
                   Process(id="read-external-fact", name="Read external fact")),
        capabilities=(
            Capability(id="one-unit-service", name="One-unit prototype service", visibility="public",
                       process="provide-service", resources=("material-bin",), requirements=business,
                       evidence=("service-declaration",), observations=(ObservationRequirement(
                           observation=OBSERVATION, resource="material-bin", property="has-one-unit",
                           allowed=(True,), purpose="readiness", on_mismatch="conditional",
                           remedy="Verify or arrange the required material in the external system.",
                       ),)),
            Capability(id="external-read-policy", name="External read policy", process="read-external-fact",
                       requirements=access, evidence=("access-declaration",)),
        ),
        evidence=(
            Evidence(id="service-declaration", name="Example service declaration", level="declared",
                     source="simulated", claim="Example capability and prototype policy.",
                     capabilities=("one-unit-service",), resources=("material-bin",),
                     requirements=business, recorded_at=AT),
            Evidence(id="access-declaration", name="Declared access policy", level="declared",
                     source="person", claim="Only the named stock record may be read for shop readiness.",
                     capabilities=("external-read-policy",), requirements=access, recorded_at=AT),
        ),
        operations=(ShopOperation(
            id=READ_OPERATION, name="Read material stock", adapter="erp-reader", command="read-stock",
            effect="read", effect_description="Read a stock observation and its source identity.",
            inputs=(OperationField(name="record-reference", kind="text"),
                    OperationField(name="purpose", kind="text")),
            outputs=(OperationField(name="record-reference", kind="text"),
                     OperationField(name="record-revision", kind="text"),
                     OperationField(name="observed-at", kind="text"),
                     OperationField(name="available-units", kind="quantity", unit="1", required=False)),
            checks=(OperationCheck(target_kind="capability", target="external-read-policy",
                                   inputs=("record-reference", "purpose"), minimum_evidence="declared"),),
        ),),
    )


def read_stock(shop, *, request_key, mode):
    """Submit once; callers inspect the durable execution in a later run."""
    current = shop.operation(READ_OPERATION)
    return shop.execute(
        READ_OPERATION, request_key=request_key, expected_generation=current["generation"], mode=mode,
        inputs=(Fact(name="record-reference", value=STOCK_RECORD), Fact(name="purpose", value="shop-readiness")),
    )


def stock_observation(receipt):
    """Project a completed read while preserving the source's time and simulation."""
    receipt = ExecutionRecord.model_validate(receipt)
    if (receipt.shop_id, receipt.operation_id, receipt.state) != (SHOP, READ_OPERATION, "succeeded"):
        raise ValueError("A completed stock read for this shop is required")
    if receipt.result is None or receipt.result.state != "succeeded":
        raise ValueError("The read has no confirmed output")
    output = {fact.name: fact.value for fact in receipt.result.outputs}
    required = {"record-reference", "record-revision", "observed-at"}
    if not required <= output.keys() or output.keys() - required - {"available-units"}:
        raise ValueError("Unexpected stock response fields")
    if output["record-reference"] != STOCK_RECORD:
        raise ValueError("The response identifies another external record")
    amount = output.get("available-units")
    if amount is not None and (
        not isinstance(amount, Quantity) or amount.unit != "1"
        or amount.base_value() < 0 or amount.base_value().denominator != 1
    ):
        raise ValueError("Stock must be a nonnegative whole-unit quantity or unknown")
    return Observation(
        id=OBSERVATION, resource="material-bin", property="has-one-unit",
        value=None if amount is None else amount.base_value() >= 1,
        time=output["observed-at"], max_age_seconds=MAX_AGE,
        source="simulated" if receipt.mode == "simulated" else "calculated",
        external=ExternalReference(system=SYSTEM, record=STOCK_RECORD, revision=output["record-revision"]),
    )


def sync_stock(shop, execution_id):
    """Merge one source-owned observation through revision-guarded SDK calls."""
    if shop.id != SHOP:
        raise ValueError("This mapping belongs to another shop")
    receipt = ExecutionRecord.model_validate(shop.execution(READ_OPERATION, execution_id))
    incoming = stock_observation(receipt)
    current_operation = shop.operation(READ_OPERATION)
    if (receipt.publication_generation, receipt.model_sha256, receipt.binding_generation) != (
        current_operation["generation"], current_operation["model_sha256"], current_operation["binding_generation"]
    ) or not current_operation["enabled"]:
        raise ValueError("The read's publication or adapter binding changed")
    for _ in range(3):
        snapshot = ObservationSnapshot.model_validate(shop.observations())
        values = {value.id: value for value in snapshot.observations}
        previous = values.get(incoming.id)
        if previous is not None:
            if previous.external is None or (
                previous.external.system, previous.external.record, previous.resource, previous.property
            ) != (SYSTEM, STOCK_RECORD, incoming.resource, incoming.property):
                raise ValueError("The observation ID belongs to another source")
            if previous == incoming:
                return {"state": "unchanged", "revision": snapshot.revision}
            if previous.external.revision == incoming.external.revision:
                raise ValueError("An external revision returned inconsistent data")
            if incoming.time < previous.time:
                return {"state": "ignored-older", "revision": snapshot.revision}
            if incoming.time == previous.time:
                raise ValueError("Conflicting external revisions have the same observation time")
        values[incoming.id] = incoming
        try:
            changed = shop.set_observations(tuple(values.values()), expected_revision=snapshot.revision)
            return {"state": "updated", "revision": changed["revision"]}
        except ServiceError as error:
            if error.status != 409:
                raise
            # A definite conflict permits a bounded re-read and merge. A lost
            # response propagates; an explicit retry reconciles the snapshot.
    raise ValueError("Concurrent observation changes prevented synchronization")


def request(*, purpose="readiness", engagement="prototype", count="1", allow_simulated=True):
    return EvaluationRequest(target_kind="capability", target="one-unit-service", purpose=purpose,
                             facts=(Fact(name="engagement", value=engagement), Fact(name="quantity", value=quantity(count, "1"))),
                             allow_simulated=allow_simulated)


def sample_observation(**changes):
    values = dict(id=OBSERVATION, resource="material-bin", property="has-one-unit", value=True,
                  time=AT, max_age_seconds=MAX_AGE, source="simulated",
                  external=ExternalReference(system=SYSTEM, record=STOCK_RECORD, revision="stock-v1"))
    return Observation(**(values | changes))


def cases():
    seen = sample_observation()
    reasons = {
        "installed-without-stock": "model-supports-request", "fresh-stock": "model-supports-request",
        "no-stock": "observation-condition", "missing-stock": "observation-missing",
        "stale-stock": "observation-stale", "unknown-stock": "observation-unknown",
        "future-stock": "observation-future", "production-policy": "value-not-supported",
        "quantity-policy": "outside-range", "simulation-is-not-live": "observation-simulated",
    }
    return tuple(AcceptanceCase(id=name, request=query, observations=observations, expected=expected,
                               at=at, reason_codes=(reasons[name],))
                 for name, query, observations, expected, at in (
                     ("installed-without-stock", request(purpose="capability"), (), "supported", AT),
                     ("fresh-stock", request(), (seen,), "supported", AT),
                     ("no-stock", request(), (sample_observation(value=False),), "conditional", AT),
                     ("missing-stock", request(), (), "unknown", AT),
                     ("stale-stock", request(), (seen,), "unknown", AT + timedelta(seconds=61)),
                     ("unknown-stock", request(), (sample_observation(value=None),), "unknown", AT),
                     ("future-stock", request(), (sample_observation(time=AT + timedelta(seconds=1)),), "unknown", AT),
                     ("production-policy", request(engagement="production"), (seen,), "unsupported", AT),
                     ("quantity-policy", request(count="2"), (seen,), "unsupported", AT),
                     ("simulation-is-not-live", request(allow_simulated=False), (seen,), "unknown", AT),
                 ))


def project():
    return SourceProject(shop_id=SHOP, entrypoint="shop:define_shop",
                         files=(SourceFile(path="shop.py", content=Path(__file__).read_text()),), tests=cases())


if __name__ == "__main__":
    source = project()
    artifact = compile_shop(define_shop(), BuildIdentity(source_sha256=source.revision,
                            sdk_sha256="0" * 64, dependencies_sha256="0" * 64, runtime="trusted-example"))
    results = run_cases(artifact, source.tests)
    for case in results:
        assert case.passed, case
    print(f"ERP interoperability: {len(results)} technical/business/freshness cases passed")
