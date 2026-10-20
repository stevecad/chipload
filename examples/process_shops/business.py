"""Executable example business conditions plus contextual descriptive policies.

Commercial acceptance uses ordinary capabilities/workflows. It is separate from
physical feasibility. This is an example library choice, not an engine shop type.
"""

from dataclasses import dataclass

from chipload import Capability, Choice, Fact, Range, ShopVariable, ValueSet, quantity

from .definitions import AT, facts


@dataclass(frozen=True)
class Terms:
    id: str
    minimum_production: str
    maximum_prototype: str
    customer_stock: bool
    outside_processing: bool
    releases: bool
    typical_hours: str


# Fictional examples of different business models, not defaults for an industry.
TERMS = {
    "job-shop": Terms("job-shop", "25", "10", True, False, True, "120"),
    "production": Terms("production", "100", "5", True, False, True, "240"),
    "batch": Terms("batch", "20", "8", True, True, True, "168"),
    "project": Terms("project", "2", "1", False, True, False, "360"),
    "inspection": Terms("inspection", "10", "5", True, False, True, "72"),
}


def conditions(terms: Terms, engagement: str) -> Capability:
    prototype = engagement == "prototype"
    rules = (
        Choice(
            fact="engagement",
            allowed=("prototype",) if prototype else ("production", "annual-contract"),
        ),
        Range(
            fact="lot-quantity",
            minimum=quantity("1" if prototype else terms.minimum_production, "1"),
            maximum=quantity(terms.maximum_prototype if prototype else "10000", "1"),
            multiple_of=quantity("1", "1"),
        ),
        Choice(
            fact="input-ownership",
            allowed=("customer-supplied", "shop-supplied")
            if terms.customer_stock
            else ("shop-supplied",),
        ),
        Choice(
            fact="input-records-provided",
            allowed=(True,),
            on_missing="conditional",
            on_mismatch="conditional",
            remedy="Supply the incoming material/component records required by the shop.",
        ),
        Choice(
            fact="delivery-arrangement",
            allowed=("single-delivery", "monthly-releases")
            if terms.releases and not prototype
            else ("single-delivery",),
        ),
        Choice(fact="documentation-package", allowed=("standard-process-records",)),
        Choice(fact="customer-requirements-reviewed", allowed=(True,), on_mismatch="unknown"),
        Choice(
            fact="schedule-confirmed",
            allowed=(True,),
            on_missing="conditional",
            on_mismatch="conditional",
            remedy="Confirm an actual delivery commitment with the shop.",
        ),
    )
    if terms.outside_processing:
        rules += (
            Choice(
                fact="outside-processing-authorized",
                allowed=(True,),
                on_missing="conditional",
                on_mismatch="conditional",
                remedy="Authorize the declared outside-processing arrangement.",
            ),
        )
    return Capability(
        id=f"terms-{engagement}",
        name=f"{engagement.title()} acceptance conditions",
        process="engagement-conditions",
        visibility="public",
        description=(
            "Fictional commercial acceptance conditions, evaluated independently of technical "
            "feasibility. This does not create a quote, order, reservation, or contract."
        ),
        requirements=rules,
        evidence=(f"terms-{engagement}-declaration",),
        output_state=(Fact(name="business-conditions", value="satisfied"),),
    )


def inputs(terms: Terms, engagement: str):
    result = facts(
        engagement=engagement,
        lot_quantity=quantity("1" if engagement == "prototype" else "250", "1"),
        input_ownership="shop-supplied",
        input_records_provided=True,
        delivery_arrangement="single-delivery",
        documentation_package="standard-process-records",
        customer_requirements_reviewed=True,
        schedule_confirmed=True,
    )
    if terms.outside_processing:
        result += (Fact(name="outside-processing-authorized", value=True),)
    return result


def variables(terms: Terms) -> tuple[ShopVariable, ...]:
    def variable(id, name, category, value, *, certainty="declared", when=(), notes=""):
        return ShopVariable(
            id=id,
            name=name,
            category=category,
            value=value,
            certainty=certainty,
            source="simulated",
            recorded_at=AT,
            visibility="public",
            applies_when=when,
            notes=notes,
        )

    prototype = (Choice(fact="engagement", allowed=("prototype",)),)
    production = (Choice(fact="engagement", allowed=("production", "annual-contract")),)
    return (
        variable(
            "minimum-production-lot",
            "Minimum production lot",
            "business-policy",
            quantity(terms.minimum_production, "1"),
            when=production,
            notes="Enforced by terms-production; the prototype offer has separate quantity limits.",
        ),
        variable(
            "maximum-prototype-lot",
            "Maximum prototype lot",
            "business-policy",
            quantity(terms.maximum_prototype, "1"),
            when=prototype,
            notes="The prototype route is an explicit exception to the production minimum.",
        ),
        variable(
            "customer-supplied-inputs",
            "Customer-supplied material or components",
            "business-policy",
            terms.customer_stock,
            notes="Incoming records and customer requirements still need review.",
        ),
        variable(
            "scheduled-releases",
            "Scheduled release arrangements",
            "production-planning",
            terms.releases,
            when=production,
        ),
        variable(
            "finished-goods-storage",
            "Finished goods storage",
            "production-planning",
            terms.releases,
            when=(Choice(fact="engagement", allowed=("annual-contract",)),),
            notes="An offered arrangement, not a stock or capacity reservation.",
        ),
        variable(
            "outside-processing",
            "Outside processing is part of the offered arrangement",
            "business-policy",
            terms.outside_processing,
            notes="When true, the acceptance conditions require explicit authorization.",
        ),
        variable(
            "typical-turnaround",
            "Typical processing turnaround",
            "service-expectations",
            quantity(terms.typical_hours, "h"),
            certainty="typical",
            notes="Fictional typical elapsed time; schedule-confirmed is a separate condition.",
        ),
        variable(
            "standard-records",
            "Standard documentation",
            "documentation",
            ValueSet(values=("process-record", "input-traceability")),
            notes="Customer-specific certificates require a separately established scope.",
        ),
        variable(
            "customer-qualification",
            "Customer-specific qualification",
            "qualification",
            None,
            certainty="unknown",
        ),
        variable(
            "minimum-charge",
            "Minimum charge",
            "commercial-terms",
            None,
            certainty="unknown",
            notes="No price is established by these examples; quantity policy is not a quotation.",
        ),
        variable(
            "annual-capacity",
            "Available annual capacity",
            "production-planning",
            None,
            certainty="unknown",
            notes="Published process capability does not establish available annual throughput.",
        ),
    )
