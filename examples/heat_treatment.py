"""Heat-treatment-only shop expressed through the generic SDK.

All equipment, limits, cycles, and evidence below are fictional test data.
This example is not a metallurgical recipe or an equipment-control program.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from chipload import (
    Capability,
    Catalog,
    CatalogItem,
    Choice,
    EvaluationRequest,
    Evidence,
    Fact,
    Observation,
    ObservationRequirement,
    Process,
    Range,
    Resource,
    Shop,
    ShopVariable,
    Step,
    ValueSet,
    Workflow,
    quantity,
)

AT = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


def define_shop() -> Shop:
    inputs = (
        Choice(fact="alloy", catalog="alloys", allowed=("demo-alloy",)),
        Choice(fact="incoming-condition", allowed=("documented",)),
        Choice(fact="load-plan-approved", allowed=(True,), on_mismatch="unknown"),
        Choice(fact="cycle-approved", allowed=(True,), on_mismatch="unknown"),
        Range(fact="loaded-batch-mass", minimum=quantity("1", "kg"), maximum=quantity("400", "kg")),
        Range(fact="load-width", minimum=quantity("1", "mm"), maximum=quantity("600", "mm")),
        Range(
            fact="cycle-temperature", minimum=quantity("100", "Cel"), maximum=quantity("700", "Cel")
        ),
        Range(fact="cycle-duration", minimum=quantity("1", "min"), maximum=quantity("12", "h")),
    )
    thermal = Capability(
        id="thermal-cycle",
        name="Approved thermal cycle",
        process="thermal-processing",
        visibility="public",
        resources=("furnace-one",),
        requirements=inputs,
        evidence=("thermal-declaration",),
        input_state=(Fact(name="traceability", value="identified"),),
        output_state=(Fact(name="processing-stage", value="heated"),),
        observations=(
            ObservationRequirement(
                observation="furnace-calibration",
                resource="furnace-one",
                property="calibration",
                allowed=("valid",),
                remedy="Establish calibration applicable to this process before claiming support.",
            ),
        ),
        description="Fictional envelope, conditional on independent cycle and load-plan review.",
    )
    cooling = Capability(
        id="controlled-cooling",
        name="Controlled cooling",
        process="cooling",
        visibility="public",
        resources=("cooling-bath",),
        requirements=(Choice(fact="cooling-medium", catalog="media", allowed=("demo-medium",)),),
        evidence=("cooling-declaration",),
        input_state=(Fact(name="processing-stage", value="heated"),),
        output_state=(Fact(name="processing-stage", value="cooled"),),
    )
    inspection = Capability(
        id="verify-batch",
        name="Batch verification",
        process="verification",
        visibility="public",
        resources=("inspection-station",),
        requirements=(
            Choice(fact="inspection-plan-approved", allowed=(True,), on_mismatch="unknown"),
        ),
        evidence=("verification-declaration",),
        input_state=(Fact(name="processing-stage", value="cooled"),),
        output_state=(Fact(name="processing-stage", value="verified"),),
    )
    capabilities = (thermal, cooling, inspection)
    evidence = tuple(
        Evidence(
            id=capability.evidence[0],
            name=f"Simulation for {capability.name}",
            level="declared",
            source="simulated",
            recorded_at=AT,
            claim="Fictional test declaration; not a heat-treatment qualification.",
            capabilities=(capability.id,),
            resources=capability.resources,
            requirements=tuple(
                rule.model_copy(update={"on_mismatch": "unsupported", "on_missing": "unknown"})
                for rule in capability.requirements
            ),
        )
        for capability in capabilities
    )
    return Shop(
        id="thermal-example",
        name="Thermal Processing Example",
        visibility="public",
        description="A heat-treatment-only example with no machining resources.",
        resources=(
            Resource(
                id="furnace-one",
                name="Example furnace",
                kind="controlled-atmosphere-furnace",
                visibility="public",
            ),
            Resource(
                id="cooling-bath", name="Example bath", kind="cooling-bath", visibility="public"
            ),
            Resource(
                id="inspection-station",
                name="Example inspection station",
                kind="batch-verification-station",
                visibility="public",
            ),
        ),
        catalogs=(
            Catalog(
                id="alloys",
                name="Alloy vocabulary",
                visibility="public",
                items=(CatalogItem(id="demo-alloy", name="Fictional alloy", visibility="public"),),
            ),
            Catalog(
                id="media",
                name="Cooling media",
                visibility="public",
                items=(
                    CatalogItem(id="demo-medium", name="Fictional medium", visibility="public"),
                ),
            ),
        ),
        processes=tuple(
            Process(id=id, name=name, visibility="public")
            for id, name in (
                ("thermal-processing", "Thermal processing"),
                ("cooling", "Controlled cooling"),
                ("verification", "Batch verification"),
            )
        ),
        capabilities=capabilities,
        evidence=evidence,
        workflows=(
            Workflow(
                id="treat-and-verify",
                name="Treat, cool, and verify",
                visibility="public",
                steps=(
                    Step(id="heat", capability="thermal-cycle"),
                    Step(id="cool", capability="controlled-cooling", after=("heat",)),
                    Step(id="verify", capability="verify-batch", after=("cool",)),
                ),
            ),
            Workflow(
                id="incorrect-sequence",
                name="Invalid inspection sequence",
                steps=(
                    Step(id="heat", capability="thermal-cycle"),
                    Step(id="verify", capability="verify-batch", after=("heat",)),
                ),
            ),
        ),
        variables=(
            ShopVariable(
                id="available-records",
                name="Available process records",
                category="documentation",
                value=ValueSet(values=("cycle-record", "lot-traceability", "inspection-report")),
                certainty="declared",
                source="simulated",
                recorded_at=AT,
                visibility="public",
            ),
            ShopVariable(
                id="customer-qualification",
                name="Customer-specific qualification",
                category="business-policy",
                value=None,
                certainty="unknown",
                source="simulated",
                recorded_at=AT,
                visibility="public",
                notes="Qualification must be established for the customer's actual requirements.",
            ),
        ),
    )


def request(
    *, target: str = "thermal-cycle", workflow: bool = False, changes: dict | None = None
) -> EvaluationRequest:
    facts = {
        "alloy": "demo-alloy",
        "incoming-condition": "documented",
        "load-plan-approved": True,
        "cycle-approved": True,
        "loaded-batch-mass": quantity("100", "kg"),
        "load-width": quantity("300", "mm"),
        "cycle-temperature": quantity("600", "Cel"),
        "cycle-duration": quantity("2", "h"),
    }
    if workflow:
        facts["inspection-plan-approved"] = True
        if target == "treat-and-verify":
            facts["cooling-medium"] = "demo-medium"
    for key, value in (changes or {}).items():
        if value is None:
            facts.pop(key, None)
        else:
            facts[key] = value
    return EvaluationRequest(
        target_kind="workflow" if workflow else "capability",
        target=target,
        facts=tuple(Fact(name=k, value=v) for k, v in facts.items()),
        initial_state=(Fact(name="traceability", value="identified"),),
        allow_simulated=True,
    )


def observations() -> tuple[Observation, ...]:
    return (
        Observation(
            id="furnace-calibration",
            resource="furnace-one",
            property="calibration",
            value="valid",
            time=AT,
            max_age_seconds=3600,
            source="simulated",
        ),
    )


@dataclass(frozen=True)
class Scenario:
    name: str
    question: EvaluationRequest
    expected: str
    reason: str


SCENARIOS = (
    Scenario("approved-cycle", request(), "supported", "model-supports-request"),
    Scenario(
        "overweight-batch",
        request(changes={"loaded-batch-mass": quantity("401", "kg")}),
        "unsupported",
        "outside-range",
    ),
    Scenario(
        "unreviewed-cycle",
        request(changes={"cycle-approved": False}),
        "unknown",
        "value-not-supported",
    ),
    Scenario(
        "missing-incoming-condition",
        request(changes={"incoming-condition": None}),
        "unknown",
        "missing-fact",
    ),
    Scenario(
        "unsupported-alloy",
        request(changes={"alloy": "another-alloy"}),
        "unsupported",
        "value-not-supported",
    ),
    Scenario(
        "temperature-at-limit",
        request(changes={"cycle-temperature": quantity("1292", "[degF]")}),
        "supported",
        "model-supports-request",
    ),
    Scenario(
        "unmodeled-hardness",
        request(changes={"hardness": "customer-required-result"}),
        "unknown",
        "unmodeled-requirement",
    ),
    Scenario(
        "complete-cycle",
        request(target="treat-and-verify", workflow=True),
        "supported",
        "model-supports-request",
    ),
    Scenario(
        "wrong-sequence",
        request(target="incorrect-sequence", workflow=True),
        "unsupported",
        "incompatible-input-state",
    ),
)


def main() -> None:
    import argparse
    import json
    from pathlib import Path

    from chipload import evaluate
    from chipload.discovery import public_catalog
    from examples.common import build_example

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public", action="store_true", help="print the public shop catalog")
    args = parser.parse_args()
    artifact = build_example(define_shop, (Path(__file__).resolve(),))
    if args.public:
        print(json.dumps(public_catalog(artifact), indent=2))
        return
    failures = []
    for scenario in SCENARIOS:
        result = evaluate(artifact, scenario.question, at=AT, observations=observations())
        correct = result.outcome == scenario.expected and scenario.reason in {
            reason.code for reason in result.reasons
        }
        print(f"{'PASS' if correct else 'FAIL'} {scenario.name}: {result.outcome}")
        if not correct:
            failures.append(scenario.name)
            print(result.model_dump_json(indent=2))
    if failures:
        raise SystemExit(1)
    print(f"All {len(SCENARIOS)} scenarios passed; model {artifact.model_sha256}.")


if __name__ == "__main__":
    main()
