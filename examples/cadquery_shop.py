"""A simulated fixture and its business conditions, defined with Python geometry."""

from datetime import datetime, timezone
from pathlib import Path

from chipload import (
    Capability,
    Choice,
    EvaluationRequest,
    Evidence,
    Fact,
    Process,
    Range,
    Resource,
    Shop,
    Step,
    Workflow,
    quantity,
)
from chipload.project import AcceptanceCase, SourceFile, SourceProject

AT = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


def define_shop():
    import cadquery as cq

    # This CAD solid describes the available fixture space in millimetres.
    # It establishes a geometric envelope, not cutting loads or machinability.
    envelope = cq.Workplane("XY").box(120, 80, 40).val().BoundingBox()
    technical = Capability(
        id="fixture-fit",
        name="Fit within the fixture envelope",
        visibility="public",
        process="fixture-setup",
        resources=("fixture",),
        evidence=("geometry",),
        requirements=tuple(
            Range(
                fact=axis, minimum=quantity("1", "mm"), maximum=quantity(str(round(size, 6)), "mm")
            )
            for axis, size in (
                ("width", envelope.xlen),
                ("depth", envelope.ylen),
                ("height", envelope.zlen),
            )
        ),
    )
    business = Capability(
        id="commercial-conditions",
        name="Prototype conditions",
        visibility="public",
        process="commercial-review",
        evidence=("policy",),
        requirements=(
            Choice(fact="arrangement", allowed=("prototype",)),
            Choice(
                fact="drawing-approved",
                allowed=(True,),
                on_mismatch="conditional",
                remedy="Approve the drawing before accepting the work.",
            ),
        ),
    )
    return Shop(
        id="cad-fixture",
        name="CAD fixture example",
        visibility="public",
        resources=(
            Resource(id="fixture", name="Parametric fixture", kind="fixture", visibility="public"),
        ),
        processes=(
            Process(id="fixture-setup", name="Fixture setup", visibility="public"),
            Process(id="commercial-review", name="Commercial review", visibility="public"),
        ),
        capabilities=(technical, business),
        evidence=tuple(
            Evidence(
                id=cap.evidence[0],
                name=cap.name,
                level="declared",
                source="simulated",
                recorded_at=AT,
                claim="Fictional example declaration.",
                capabilities=(cap.id,),
                resources=cap.resources,
                requirements=tuple(
                    # The policy describes both approval states; only the offer
                    # makes an unapproved drawing conditional on later review.
                    Choice(fact=rule.fact, allowed=(True, False))
                    if rule.fact == "drawing-approved"
                    else rule.model_copy(
                        update={"on_mismatch": "unsupported", "on_missing": "unknown"}
                    )
                    for rule in cap.requirements
                ),
            )
            for cap in (technical, business)
        ),
        workflows=(
            Workflow(
                id="prototype-setup",
                name="Prototype fixture setup",
                visibility="public",
                steps=(
                    Step(id="terms", capability=business.id),
                    Step(id="fit", capability=technical.id, after=("terms",)),
                ),
            ),
        ),
    )


def project(dependencies_sha256):
    """Export without importing CadQuery on the agent's or operator's host."""
    cases = []
    for name, changes, outcome in (
        ("fits", {}, "supported"),
        ("oversize", {"width": quantity("121", "mm")}, "unsupported"),
        ("business-rejection", {"arrangement": "annual-contract"}, "unsupported"),
        ("drawing-review", {"drawing-approved": False}, "conditional"),
        ("unknown-height", {"height": None}, "unknown"),
    ):
        facts = dict(
            width=quantity("100", "mm"),
            depth=quantity("60", "mm"),
            height=quantity("20", "mm"),
            arrangement="prototype",
            **{"drawing-approved": True},
        )
        facts.update(changes)
        cases.append(
            AcceptanceCase(
                id=name,
                at=AT,
                expected=outcome,
                request=EvaluationRequest(
                    target_kind="workflow",
                    target="prototype-setup",
                    allow_simulated=True,
                    facts=tuple(Fact(name=k, value=v) for k, v in facts.items() if v is not None),
                ),
            )
        )
    cases.append(
        cases[0].model_copy(
            update={
                "id": "simulation-not-accepted",
                "expected": "unknown",
                "request": cases[0].request.model_copy(update={"allow_simulated": False}),
            }
        )
    )
    return SourceProject(
        shop_id="cad-fixture",
        entrypoint="shop:define_shop",
        dependencies_sha256=dependencies_sha256,
        files=(SourceFile(path="shop.py", content=Path(__file__).read_text()),),
        tests=tuple(cases),
    )


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Export a source project pinned to a worker dependency digest."
    )
    parser.add_argument("dependencies_sha256")
    args = parser.parse_args()
    sys.stdout.buffer.write(project(args.dependencies_sha256).to_json() + b"\n")
