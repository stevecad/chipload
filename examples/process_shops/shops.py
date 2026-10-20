"""Build complete shop projects from ordinary reusable Python components."""

from chipload import (
    Capability,
    Catalog,
    CatalogItem,
    Choice,
    Evidence,
    Fact,
    ObservationRequirement,
    Process,
    Resource,
    Shop,
    Step,
    Workflow,
)

from .business import TERMS, conditions, variables
from .definitions import AT, ProcessExample


def declaration(capability: Capability) -> Evidence:
    return Evidence(
        id=capability.evidence[0],
        name=f"Simulated declaration for {capability.name}",
        level="declared",
        source="simulated",
        claim="Fictional acceptance fixture; not a physical or customer qualification.",
        capabilities=(capability.id,),
        resources=capability.resources,
        requirements=tuple(
            rule.model_copy(update={"on_mismatch": "unsupported", "on_missing": "unknown"})
            for rule in capability.requirements
        ),
        recorded_at=AT,
    )


def components(example: ProcessExample):
    resources = (
        Resource(
            id=f"{example.id}-station",
            name=example.name + " station",
            kind=example.machine_kind,
            visibility="public",
            private_details=(
                Fact(name="controller-address", value="https://private-equipment.example.invalid"),
            ),
        ),
        Resource(
            id=f"{example.id}-tooling",
            name=example.tooling,
            kind="process-tooling",
            visibility="public",
        ),
        Resource(
            id=f"{example.id}-fixture",
            name="Reviewed workholding or process fixture",
            kind="process-fixture",
            visibility="public",
        ),
    )
    capability = Capability(
        id=example.id,
        name=example.name,
        process=example.id,
        visibility="public",
        description=example.limitation + " All limits and evidence are fictional.",
        resources=tuple(resource.id for resource in resources),
        requirements=(
            Choice(fact="input-grade", catalog="input-grades", allowed=("demo-approved-input",)),
            *example.requirements,
            Choice(
                fact=f"{example.id}-setup",
                allowed=("verified",),
                on_missing="conditional",
                on_mismatch="conditional",
                remedy="Prepare and verify the declared tooling and workholding setup.",
            ),
        ),
        input_state=(Fact(name="traceability", value="identified"),),
        output_state=(Fact(name=f"{example.id}-stage", value="processed"),),
        observations=(
            ObservationRequirement(
                observation=f"{example.id}-verification",
                resource=resources[0].id,
                property="process-verification",
                allowed=("current",),
                remedy="Establish current verification applicable to this process.",
            ),
            ObservationRequirement(
                observation=f"{example.id}-tooling-ready",
                resource=resources[1].id,
                property="available",
                allowed=(True,),
                purpose="readiness",
                remedy="Schedule or restore the declared tooling before starting work.",
            ),
        ),
        evidence=(f"{example.id}-declaration",),
    )
    return resources, capability


def define_shop(example: ProcessExample) -> Shop:
    terms = TERMS[example.policy]
    resources, physical = components(example)
    business = tuple(conditions(terms, engagement) for engagement in ("prototype", "production"))
    capabilities = (physical, *business)
    return Shop(
        id=example.shop_id,
        name=f"{example.name} Example Shop",
        visibility="public",
        description=(
            "Fictional software-defined shop with technical and business conditions. "
            "The inputs and limits are test fixtures, not manufacturing guidance."
        ),
        resources=resources,
        catalogs=(
            Catalog(
                id="input-grades",
                name="Reviewed inputs",
                visibility="public",
                items=(
                    CatalogItem(
                        id="demo-approved-input",
                        name="Fictional reviewed input",
                        visibility="public",
                    ),
                ),
            ),
        ),
        processes=(
            Process(id=example.id, name=example.name, visibility="public", delivery="in-house"),
            Process(
                id="engagement-conditions",
                name="Business acceptance conditions",
                visibility="public",
            ),
        ),
        capabilities=capabilities,
        evidence=tuple(declaration(capability) for capability in capabilities),
        workflows=tuple(
            Workflow(
                id=f"offer-{engagement}",
                name=f"{engagement.title()} {example.name}",
                visibility="public",
                description="Checks commercial conditions and technical feasibility together.",
                steps=(
                    Step(id="terms", capability=f"terms-{engagement}"),
                    Step(id="process", capability=physical.id, after=("terms",)),
                ),
            )
            for engagement in ("prototype", "production")
        ),
        variables=variables(terms),
    )
