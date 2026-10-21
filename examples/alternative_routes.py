"""Fictional alternative routes with independent technical and business conditions."""

from datetime import datetime, timezone
from pathlib import Path

from chipload import (
    BuildIdentity,
    Capability,
    Choice,
    EvaluationRequest,
    Evidence,
    Fact,
    Offer,
    Process,
    Range,
    Resource,
    Route,
    Shop,
    Step,
    Workflow,
    compile_shop,
    evaluate,
    quantity,
)
from chipload.project import AcceptanceCase, SourceFile, SourceProject

AT = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
PROFILES = {
    "machining": dict(
        name="Machining routes",
        fact="width",
        unit="mm",
        limits=(80, 160),
        modes=("prototype", "production"),
        counts=(20, 100),
        kind="mill",
    ),
    "thermal": dict(
        name="Heat-treatment routes",
        fact="temperature",
        unit="Cel",
        limits=(200, 600),
        modes=("trial", "scheduled-batch"),
        counts=(10, 20),
        kind="furnace",
    ),
    "review": dict(
        name="Document review routes",
        fact="pages",
        unit="1",
        limits=(10, 1000),
        modes=("one-off", "retainer"),
        counts=(2, 10),
        kind=None,
    ),
}


def define_shop(profile="machining"):
    config = PROFILES[profile]
    technical, workflows = [], []
    for index, identifier in enumerate(("small-lot", "contract")):
        technical.append(
            Capability(
                id=identifier,
                name=identifier.replace("-", " ").title(),
                visibility="public",
                process="processing",
                resources=(identifier,) if config["kind"] else (),
                requirements=(
                    Range(
                        fact=config["fact"],
                        minimum=quantity("1", config["unit"]),
                        maximum=quantity(str(config["limits"][index]), config["unit"]),
                    ),
                ),
                evidence=(identifier + "-declaration",),
                output_state=(Fact(name="stage", value="processed"),),
            )
        )
        workflows.append(
            Workflow(
                id=identifier + "-route",
                name=identifier.replace("-", " ").title() + " route",
                visibility="public",
                requirements=(
                    Choice(fact="engagement", allowed=(config["modes"][index],)),
                    Range(
                        fact="quantity",
                        minimum=quantity("1" if index == 0 else str(config["counts"][1]), "1"),
                        maximum=quantity(str(config["counts"][0]) if index == 0 else "10000", "1"),
                        multiple_of=quantity("1", "1"),
                    ),
                ),
                steps=(
                    Step(id="process", capability=identifier),
                    Step(id="inspect", capability="inspection", after=("process",)),
                ),
            )
        )
    inspection = Capability(
        id="inspection",
        name="Verify result",
        visibility="public",
        process="verification",
        input_state=(Fact(name="stage", value="processed"),),
        evidence=("inspection-declaration",),
    )
    capabilities = (*technical, inspection)
    return Shop(
        id="routes-" + profile,
        name=config["name"],
        visibility="public",
        description="Simulated routes; each includes technical limits and business conditions.",
        processes=(
            Process(id="processing", name="Processing", visibility="public"),
            Process(id="verification", name="Verification", visibility="public"),
        ),
        resources=(
            Resource(
                id="small-lot", name="Internal resource", kind=config["kind"], visibility="public"
            ),
            Resource(
                id="contract", name="External service", kind="external-service", visibility="public"
            ),
        )
        if config["kind"]
        else (),
        capabilities=capabilities,
        evidence=tuple(
            Evidence(
                id=cap.evidence[0],
                name=cap.name + " declaration",
                level="declared",
                source="simulated",
                claim="Fictional modeled scope, not physical qualification.",
                recorded_at=AT,
                capabilities=(cap.id,),
                resources=cap.resources,
                requirements=cap.requirements,
            )
            for cap in capabilities
        ),
        workflows=tuple(workflows),
        offers=(
            Offer(
                id="service",
                name="Shop service",
                visibility="public",
                requirements=(
                    Choice(
                        fact="terms-accepted",
                        allowed=(True,),
                        on_mismatch="conditional",
                        remedy="Accept the published terms before proceeding.",
                    ),
                ),
                alternatives=tuple(
                    Route(target_kind="workflow", target=workflow.id) for workflow in workflows
                ),
            ),
        ),
    )


def define_machining():
    return define_shop("machining")


def define_thermal():
    return define_shop("thermal")


def define_review():
    return define_shop("review")


def cases(profile):
    config = PROFILES[profile]
    base = {
        config["fact"]: quantity(str(config["limits"][0] // 2), config["unit"]),
        "engagement": config["modes"][0],
        "quantity": quantity("1", "1"),
        "terms-accepted": True,
    }
    scenarios = (
        ("small-lot", {}, "supported"),
        (
            "contract",
            {
                "engagement": config["modes"][1],
                "quantity": quantity(str(config["counts"][1]), "1"),
                config["fact"]: quantity(str(config["limits"][1]), config["unit"]),
            },
            "supported",
        ),
        ("contract-below-minimum", {"engagement": config["modes"][1]}, "unsupported"),
        (
            "outside-all-routes",
            {config["fact"]: quantity(str(config["limits"][1] + 1), config["unit"])},
            "unsupported",
        ),
        ("terms-needed", {"terms-accepted": False}, "conditional"),
        ("missing-engagement", {"engagement": None}, "unknown"),
        ("missing-technical-input", {config["fact"]: None}, "unknown"),
        ("unmodeled-requirement", {"finish": "unmodeled"}, "unknown"),
    )
    return tuple(
        AcceptanceCase(
            id=name,
            at=AT,
            expected=expected,
            request=EvaluationRequest(
                target_kind="offer",
                target="service",
                allow_simulated=True,
                facts=tuple(
                    Fact(name=key, value=value)
                    for key, value in (base | changes).items()
                    if value is not None
                ),
            ),
        )
        for name, changes, expected in scenarios
    )


def project(profile):
    return SourceProject(
        shop_id="routes-" + profile,
        entrypoint="routes:define_" + profile,
        files=(SourceFile(path="routes.py", content=Path(__file__).read_text()),),
        tests=cases(profile),
    )


if __name__ == "__main__":
    for profile in PROFILES:
        artifact = compile_shop(
            define_shop(profile),
            BuildIdentity(
                source_sha256=project(profile).revision,
                sdk_sha256="a" * 64,
                dependencies_sha256="b" * 64,
                runtime="trusted-example",
            ),
        )
        for case in cases(profile):
            result = evaluate(artifact, case.request, at=case.at)
            assert result.outcome == case.expected, (profile, case.id, result)
        print(f"{profile}: {len(cases(profile))} alternative-route cases passed")
