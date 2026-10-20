"""A simulated shop with machine, human, inspection, and external-service calls."""

from datetime import datetime, timezone
from pathlib import Path

from chipload import (
    Capability,
    Choice,
    Evidence,
    Fact,
    OperationCheck,
    OperationField,
    Process,
    Range,
    Resource,
    Shop,
    ShopOperation,
    Step,
    Workflow,
    quantity,
)
from chipload.evaluate import EvaluationRequest
from chipload.project import AcceptanceCase, SourceFile, SourceProject

AT = datetime(2026, 10, 20, tzinfo=timezone.utc)


def define_shop():
    technical = (Range(fact="length", minimum=quantity("1", "mm"), maximum=quantity("100", "mm")),)
    business = (
        Choice(fact="engagement", allowed=("prototype",)),
        Range(
            fact="quantity",
            minimum=quantity("1", "1"),
            maximum=quantity("10", "1"),
            multiple_of=quantity("1", "1"),
        ),
    )
    check = OperationCheck(
        target_kind="workflow",
        target="prototype-route",
        inputs=("length", "engagement", "quantity"),
    )
    policies = OperationCheck(
        target_kind="capability", target="prototype-policy", inputs=("engagement", "quantity")
    )
    business_inputs = (
        OperationField(name="engagement", kind="text"),
        OperationField(name="quantity", kind="quantity", unit="1"),
    )
    return Shop(
        id="callable-example",
        name="Callable shop example",
        visibility="public",
        resources=(
            Resource(id="machine", name="Example machine", kind="machine"),
            Resource(id="reviewer", name="Example reviewer", kind="human"),
            Resource(id="inspection", name="Example inspection station", kind="inspection"),
            Resource(id="outside-service", name="Example outside service", kind="external-service"),
        ),
        processes=(
            Process(id="manufacture", name="Example manufacturing", visibility="public"),
            Process(id="business-acceptance", name="Prototype policy"),
        ),
        capabilities=(
            Capability(
                id="machine-envelope",
                name="Example envelope",
                visibility="public",
                process="manufacture",
                resources=("machine",),
                requirements=technical,
                evidence=("machine-proof",),
            ),
            Capability(
                id="prototype-policy",
                name="Prototype acceptance",
                visibility="public",
                process="business-acceptance",
                requirements=business,
                evidence=("policy-proof",),
            ),
        ),
        evidence=(
            Evidence(
                id="machine-proof",
                name="Simulated machine qualification",
                level="qualified",
                source="simulated",
                claim="Fictional example only.",
                capabilities=("machine-envelope",),
                resources=("machine",),
                requirements=technical,
                recorded_at=AT,
            ),
            Evidence(
                id="policy-proof",
                name="Simulated policy",
                level="qualified",
                source="simulated",
                claim="Fictional prototype policy.",
                capabilities=("prototype-policy",),
                requirements=business,
                recorded_at=AT,
            ),
        ),
        workflows=(
            Workflow(
                id="prototype-route",
                name="Prototype route",
                visibility="public",
                steps=(
                    Step(id="business", capability="prototype-policy"),
                    Step(id="machine", capability="machine-envelope", after=("business",)),
                ),
            ),
        ),
        operations=(
            ShopOperation(
                id="make-sample",
                name="Make a sample",
                visibility="public",
                adapter="machine-link",
                command="make",
                effect="write",
                effect_description="Produce a sample using the configured machine adapter.",
                resources=("machine",),
                inputs=(
                    OperationField(name="length", kind="quantity", unit="mm"),
                    *business_inputs,
                ),
                outputs=(OperationField(name="completed", kind="boolean"),),
                checks=(check,),
            ),
            ShopOperation(
                id="review-document",
                name="Review a document",
                visibility="public",
                adapter="review-link",
                command="review",
                effect="write",
                effect_description="Request a documented human review.",
                resources=("reviewer",),
                inputs=(OperationField(name="document", kind="text"), *business_inputs),
                outputs=(OperationField(name="approved", kind="boolean"),),
                checks=(policies,),
            ),
            ShopOperation(
                id="inspect-sample",
                name="Inspect a sample",
                visibility="public",
                adapter="inspection-link",
                command="measure",
                effect="read",
                effect_description="Read an inspection result from the configured station.",
                resources=("inspection",),
                inputs=business_inputs,
                outputs=(OperationField(name="measured-length", kind="quantity", unit="mm"),),
                checks=(policies,),
            ),
            ShopOperation(
                id="outside-process",
                name="Request an outside process",
                visibility="public",
                adapter="external-link",
                command="request-process",
                effect="write",
                effect_description="Ask the configured external service to perform its process.",
                resources=("outside-service",),
                inputs=(OperationField(name="part-reference", kind="text"), *business_inputs),
                outputs=(OperationField(name="service-reference", kind="text"),),
                checks=(policies,),
            ),
        ),
    )


def inputs(quantity_value="3", engagement="prototype"):
    return (
        Fact(name="length", value=quantity("20", "mm")),
        Fact(name="quantity", value=quantity(quantity_value, "1")),
        Fact(name="engagement", value=engagement),
    )


def project():
    return SourceProject(
        shop_id="callable-example",
        entrypoint="shop:define_shop",
        files=(SourceFile(path="shop.py", content=Path(__file__).read_text()),),
        tests=tuple(
            AcceptanceCase(
                id=name,
                request=EvaluationRequest(
                    target_kind="workflow",
                    target="prototype-route",
                    facts=facts,
                    allow_simulated=True,
                ),
                at=AT,
                expected=expected,
            )
            for name, facts, expected in (
                ("prototype", inputs(), "supported"),
                ("large-lot", inputs("11"), "unsupported"),
                ("production", inputs(engagement="production"), "unsupported"),
                ("fractional-lot", inputs("1.5"), "unsupported"),
            )
        ),
    )


SIMULATORS = {
    "machine": (Fact(name="completed", value=True),),
    "review": (Fact(name="approved", value=True),),
    "inspection": (Fact(name="measured-length", value=quantity("20", "mm")),),
    "external": (Fact(name="service-reference", value="simulated-service-1"),),
}


if __name__ == "__main__":
    import sys

    sys.stdout.buffer.write(project().to_json() + b"\n")
