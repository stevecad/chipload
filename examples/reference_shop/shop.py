"""The source of truth for the fictional Riverside demonstration shop."""

from datetime import datetime, timezone
from pathlib import Path

from chipload import (
    Artifact,
    Capability,
    Catalog,
    CatalogItem,
    Choice,
    Evidence,
    Fact,
    ObservationRequirement,
    Process,
    Range,
    Resource,
    Shop,
    Site,
    Step,
    Workflow,
    quantity,
)
from examples.common import build_example

from .components import envelope, mill

RECORDED = datetime(2026, 10, 1, tzinfo=timezone.utc)


def define_shop() -> Shop:
    machines = tuple(
        Resource(
            id=machine,
            name=f"Example {machine}",
            kind="machine",
            site="main",
            visibility="public",
            private_details=(
                Fact(name="controller-address", value="tcp://machine.internal.invalid:502"),
                Fact(name="credential-reference", value=f"shops/riverside/{machine}"),
            ),
        )
        for machine in ("mill-a", "mill-b")
    )
    resources = (
        *machines,
        Resource(
            id="shared-endmill",
            name="Shared aluminium cutter",
            kind="tool",
            site="main",
            visibility="public",
        ),
        Resource(id="stock-al6061", name="Aluminium stock", kind="other", site="main"),
        Resource(
            id="flat-vise", name="Flat vise setup", kind="fixture", site="main", visibility="public"
        ),
        Resource(
            id="wide-fixture",
            name="Wide part fixture",
            kind="fixture",
            site="main",
            visibility="public",
        ),
        Resource(
            id="cmm", name="Coordinate inspection", kind="gauge", site="main", visibility="public"
        ),
        Resource(id="deburr-bench", name="Deburring bench", kind="station", site="main"),
        Resource(
            id="finishing-partner",
            name="Private supplier agreement",
            kind="other",
            site="main",
            private_details=(Fact(name="account", value="PRIVATE-SUPPLIER-42"),),
        ),
        Resource(id="research-cell", name="PRIVATE-CUSTOMER-PROJECT", kind="cell", site="main"),
    )
    milling = (
        mill(
            capability_id="mill-small",
            machine="mill-a",
            fixture="flat-vise",
            length="300",
            width="200",
        ),
        mill(
            capability_id="mill-wide",
            machine="mill-b",
            fixture="wide-fixture",
            length="450",
            width="250",
        ),
    )
    deburr = Capability(
        id="deburr",
        name="Manual deburring",
        visibility="public",
        process="deburring",
        resources=("deburr-bench",),
        requirements=(
            Choice(fact="material", allowed=("al-6061-t6",), catalog="materials"),
            Choice(fact="edge-plan-reviewed", allowed=(True,)),
        ),
        input_state=(Fact(name="geometry", value="machined"),),
        output_state=(Fact(name="edges", value="deburred"),),
        evidence=("deburr-declaration",),
    )
    inspection = Capability(
        id="inspect-bare",
        name="Bare part inspection",
        visibility="public",
        description="This inspection plan is scoped to a bare, deburred part.",
        process="inspection",
        resources=("cmm",),
        requirements=(
            Choice(fact="material", allowed=("al-6061-t6",), catalog="materials"),
            *envelope("450", "250", "120"),
            Range(fact="tolerance", minimum=quantity("0.01", "mm")),
            Choice(fact="inspection-plan-reviewed", allowed=(True,), on_mismatch="unknown"),
        ),
        input_state=(Fact(name="surface", value="bare"), Fact(name="edges", value="deburred")),
        output_state=(Fact(name="inspection", value="passed"),),
        observations=(
            ObservationRequirement(
                observation="cmm-calibration",
                resource="cmm",
                property="calibration",
                allowed=("valid",),
                remedy="Establish valid calibration before claiming inspection.",
            ),
        ),
        evidence=("inspect-bare-declaration",),
    )
    finishing = Capability(
        id="anodize",
        name="Outsourced anodizing",
        visibility="public",
        process="anodizing",
        resources=("finishing-partner",),
        requirements=(
            Choice(fact="material", allowed=("al-6061-t6",), catalog="materials"),
            *envelope("450", "250", "120"),
            Choice(
                fact="supplier-confirmed",
                allowed=(True,),
                on_missing="conditional",
                on_mismatch="conditional",
                remedy="Confirm the supplier's acceptance and arrange transport.",
            ),
        ),
        input_state=(Fact(name="geometry", value="machined"), Fact(name="surface", value="bare")),
        output_state=(Fact(name="surface", value="anodized"),),
        evidence=("anodize-declaration",),
    )
    capabilities = (*milling, deburr, inspection, finishing)
    # Each declaration captures the scope at the time it is made. Changing an
    # artifact's capability later does not change this recorded scope.
    evidence = tuple(
        Evidence(
            id=f"{c.id}-declaration",
            name=f"Simulated declaration for {c.id}",
            level="declared",
            source="simulated",
            claim="Fictional model exercise; no physical manufacturing claim.",
            capabilities=(c.id,),
            resources=c.resources,
            requirements=tuple(
                rule.model_copy(update={"on_missing": "unknown", "on_mismatch": "unsupported"})
                for rule in c.requirements
            ),
            recorded_at=RECORDED,
            valid_until=datetime(2027, 1, 1, tzinfo=timezone.utc),
        )
        for c in capabilities
    )
    return Shop(
        id="riverside",
        name="Riverside Example Shop",
        visibility="public",
        description="A fictional shop for testing software-defined manufacturing capabilities.",
        sites=(
            Site(
                id="main", name="Example workshop", time_zone="America/Chicago", visibility="public"
            ),
        ),
        resources=resources,
        catalogs=(
            Catalog(
                id="materials",
                name="Material vocabulary",
                visibility="public",
                items=(
                    CatalogItem(
                        id="al-6061-t6",
                        name="6061-T6 aluminium",
                        visibility="public",
                        properties=(Fact(name="specification", value="Example 6061-T6"),),
                    ),
                    CatalogItem(
                        id="ti-6al-4v",
                        name="Titanium test material",
                        visibility="public",
                        properties=(Fact(name="specification", value="Example Ti-6Al-4V"),),
                    ),
                ),
            ),
        ),
        processes=tuple(
            Process(
                id=process,
                name=process.title(),
                visibility="public",
                delivery="outsourced" if process == "anodizing" else "in-house",
            )
            for process in ("milling", "deburring", "inspection", "anodizing")
        ),
        capabilities=capabilities,
        evidence=evidence,
        workflows=(
            Workflow(
                id="finished-bracket",
                name="Machine, deburr, inspect, and anodize",
                visibility="public",
                requirements=(
                    Choice(
                        fact="finish-allowance-reviewed", allowed=(True,), on_mismatch="unknown"
                    ),
                ),
                steps=(
                    Step(id="machine", capability="mill-small"),
                    Step(id="deburr", capability="deburr", after=("machine",)),
                    Step(id="inspect", capability="inspect-bare", after=("deburr",)),
                    Step(id="finish", capability="anodize", after=("inspect",)),
                ),
            ),
            Workflow(
                id="invalid-finish-before-inspection",
                name="Deliberately incompatible route",
                steps=(
                    Step(id="machine", capability="mill-small"),
                    Step(id="deburr", capability="deburr", after=("machine",)),
                    Step(id="finish", capability="anodize", after=("deburr",)),
                    Step(id="inspect", capability="inspect-bare", after=("finish",)),
                ),
            ),
        ),
    )


def build() -> Artifact:
    """Build this trusted example; general source execution needs a future worker."""
    return build_example(define_shop, Path(__file__).parent.glob("*.py"))
