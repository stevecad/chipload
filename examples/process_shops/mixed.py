"""One shop with turning, milling, drilling, reaming, Swiss, and inspection.

Milling, drilling, and reaming deliberately share one machine. The shop defines
explicit routes and datum-transfer conditions; no machine-type inference occurs.
"""

from chipload import (
    Choice,
    EvaluationRequest,
    Fact,
    Process,
    Resource,
    Shop,
    Step,
    Workflow,
    quantity,
)

from .business import TERMS, conditions, variables
from .business import inputs as business_inputs
from .definitions import case, changed
from .library import BY_ID
from .scenarios import technical_inputs
from .shops import components, declaration
from .shops import define_shop as single_shop

SHOP_ID = "demo-mixed-shop"
ROUTE = ("cnc-turning", "milling-3axis", "drilling", "reaming", "coordinate-inspection")
PROCESSES = (*ROUTE, "swiss-turning")
SHARED = {"milling-3axis", "drilling", "reaming"}
TERMS_MODEL = TERMS["job-shop"]


def define_shop() -> Shop:
    resources = [
        Resource(
            id="shared-vmc",
            name="Shared machining center",
            kind="vertical-machining-center",
            visibility="public",
        )
    ]
    capabilities = []
    stages = dict(zip(ROUTE, ("turned", "milled", "drilled", "reamed", "inspected"), strict=True))
    before = dict(zip(ROUTE, ("blank", "turned", "milled", "drilled", "reamed"), strict=True))
    for id in PROCESSES:
        equipment, capability = components(BY_ID[id])
        if id in SHARED:
            equipment = equipment[1:]
            capability = capability.model_copy(
                update={
                    "resources": ("shared-vmc", *capability.resources[1:]),
                    "observations": tuple(
                        item.model_copy(update={"resource": "shared-vmc"})
                        if item.resource == f"{id}-station"
                        else item
                        for item in capability.observations
                    ),
                }
            )
        resources.extend(equipment)
        capability = capability.model_copy(
            update={
                "input_state": (
                    Fact(name="traceability", value="identified"),
                    Fact(name="part-stage", value=before.get(id, "blank")),
                ),
                "output_state": (Fact(name="part-stage", value=stages.get(id, "swiss-processed")),),
            }
        )
        capabilities.append(capability)
    capabilities.extend(conditions(TERMS_MODEL, kind) for kind in ("prototype", "production"))

    def route(id, name, processes, engagement="production"):
        steps = [Step(id="terms", capability=f"terms-{engagement}")]
        for index, process in enumerate(processes):
            steps.append(Step(id=f"step-{index}", capability=process, after=(steps[-1].id,)))
        return Workflow(
            id=id,
            name=name,
            visibility="public",
            steps=tuple(steps),
            requirements=(
                Choice(fact="datum-transfer-reviewed", allowed=(True,), on_mismatch="unknown"),
            ),
        )

    return Shop(
        id=SHOP_ID,
        name="Mixed Process Example Shop",
        visibility="public",
        description=(
            "Fictional machining shop. One VMC supports distinct milling, drilling, and reaming "
            "offers. Business acceptance and a reviewed process route remain explicit."
        ),
        resources=tuple(resources),
        catalogs=single_shop(BY_ID[ROUTE[0]]).catalogs,
        processes=(
            *(Process(id=id, name=BY_ID[id].name, visibility="public") for id in PROCESSES),
            Process(
                id="engagement-conditions",
                name="Business acceptance conditions",
                visibility="public",
            ),
        ),
        capabilities=tuple(capabilities),
        evidence=tuple(declaration(capability) for capability in capabilities),
        workflows=(
            route("production-route", "Turn, mill, drill, ream, and inspect", ROUTE),
            route("prototype-route", "Prototype with its own quantity terms", ROUTE, "prototype"),
            route(
                "invalid-order",
                "Deliberately incompatible route",
                ("cnc-turning", "coordinate-inspection", "milling-3axis", "drilling", "reaming"),
            ).model_copy(update={"visibility": "private"}),
            route("swiss-prototype", "Swiss prototype offer", ("swiss-turning",), "prototype"),
        ),
        variables=variables(TERMS_MODEL),
    )


def observations():
    return tuple(
        item.model_copy(update={"resource": "shared-vmc"})
        if id in SHARED and item.resource == f"{id}-station"
        else item
        for id in PROCESSES
        for item in BY_ID[id].observations()
    )


def request(*, target="production-route", changes=None):
    prototype = target in {"prototype-route", "swiss-prototype"}
    selected = ("swiss-turning",) if target == "swiss-prototype" else ROUTE
    values = {}
    for id in selected:
        for fact in technical_inputs(BY_ID[id]):
            if fact.name in values and values[fact.name] != fact.value:
                raise ValueError("mixed-shop request has conflicting inputs")
            values[fact.name] = fact.value
    values.update(
        {
            fact.name: fact.value
            for fact in business_inputs(TERMS_MODEL, "prototype" if prototype else "production")
        }
    )
    values["datum-transfer-reviewed"] = True
    return EvaluationRequest(
        target_kind="workflow",
        target=target,
        facts=changed(
            tuple(Fact(name=key, value=value) for key, value in values.items()), **(changes or {})
        ),
        initial_state=(
            Fact(name="traceability", value="identified"),
            Fact(name="part-stage", value="blank"),
        ),
        allow_simulated=True,
    )


def scenarios():
    snapshot = observations()
    return (
        case("mixed-production", request(), "supported", "model-supports-request", snapshot),
        case(
            "mixed-prototype-exception",
            request(target="prototype-route"),
            "supported",
            "model-supports-request",
            snapshot,
        ),
        case(
            "technical-fit-business-rejection",
            request(changes={"lot-quantity": quantity("1", "1")}),
            "unsupported",
            "outside-range",
            snapshot,
        ),
        case(
            "production-too-deep",
            request(changes={"hole-depth": quantity("81", "mm")}),
            "unsupported",
            "outside-range",
            snapshot,
        ),
        case(
            "incompatible-process-order",
            request(target="invalid-order"),
            "unsupported",
            "incompatible-input-state",
            snapshot,
        ),
        case(
            "datum-transfer-unproven",
            request(changes={"datum-transfer-reviewed": None}),
            "unknown",
            "missing-fact",
            snapshot,
        ),
        case(
            "delivery-not-confirmed",
            request(changes={"schedule-confirmed": False}),
            "conditional",
            "value-not-supported",
            snapshot,
        ),
        case(
            "swiss-is-another-offer",
            request(target="swiss-prototype"),
            "supported",
            "model-supports-request",
            snapshot,
        ),
    )
