"""Swiss shop variables and business rules."""

from datetime import datetime, timezone

from chipload import Choice, Interval, Process, Shop, ShopVariable, ValueSet, quantity

RECORDED = datetime(2026, 10, 20, tzinfo=timezone.utc)


def variable(
    id: str,
    name: str,
    category: str,
    value,
    *,
    certainty: str = "declared",
    notes: str = "",
    applies_when: tuple = (),
) -> ShopVariable:
    return ShopVariable(
        id=id,
        name=name,
        category=category,
        value=value,
        certainty=certainty,
        source="person",
        recorded_at=RECORDED,
        visibility="public",
        notes=notes,
        applies_when=applies_when,
    )


def define_shop() -> Shop:
    return Shop(
        id="swiss-example",
        name="Swiss shop",
        visibility="public",
        description="Swiss lathe machining and business practices.",
        # No site, machine inventory, timezone, specific grades, tolerances, or
        # certifications were supplied. Leave them unknown.
        processes=(
            Process(
                id="swiss-machining",
                name="Swiss lathe machining",
                visibility="public",
                description="Simple pins through prismatic parts with milled features.",
            ),
        ),
        variables=(
            variable(
                "reported-part-diameter",
                "Reported part diameter",
                "manufacturing",
                Interval(minimum=quantity("1", "mm"), maximum=quantity("32", "mm")),
                certainty="approximate",
                notes="The same response also gives a maximum of 1.25 inches. "
                "Confirm the controlling upper limit before treating either as exact.",
            ),
            variable(
                "reported-max-diameter-inch",
                "Reported maximum diameter in inches",
                "manufacturing",
                quantity("1.25", "[in_i]"),
                certainty="approximate",
                notes="1.25 inches is 31.75 mm; preserve the difference from the reported 32 mm.",
            ),
            variable(
                "usual-maximum-length",
                "Usual maximum part length",
                "manufacturing",
                Interval(minimum=quantity("8", "[in_i]"), maximum=quantity("9", "[in_i]")),
                certainty="typical",
                notes="This range describes the usual upper limit, not a minimum part length. "
                "Diameter-to-length relationships, support, and setups remain unspecified.",
            ),
            variable(
                "material-families",
                "Reported material families",
                "materials",
                ValueSet(
                    values=(
                        "alloy-steels",
                        "aluminium",
                        "stainless-steels",
                        "titanium",
                        "exotic-metals",
                    )
                ),
                notes="Broad families do not qualify every alloy, grade, condition, or feature.",
            ),
            variable(
                "excluded-material-families",
                "Materials the shop avoids",
                "business-policy",
                ValueSet(values=("plastics", "embedded-glass-fibre-composites")),
            ),
            variable("prototype-work", "Prototype work", "business-policy", True),
            variable("small-batches", "Small batches", "business-policy", True),
            variable(
                "minimum-order-quantity",
                "Minimum order quantity",
                "business-policy",
                None,
                certainty="unknown",
                notes="Small quantities were welcomed without a number.",
            ),
            variable("annual-contracts", "Annual contracts", "business-policy", True),
            variable(
                "monthly-releases",
                "Monthly release schedules",
                "production-planning",
                True,
                applies_when=(Choice(fact="engagement", allowed=("annual-contract",)),),
                notes="An offered arrangement; not a requirement for all customers.",
            ),
            variable(
                "finished-goods-inventory",
                "Finished goods inventory service",
                "business-policy",
                True,
                notes="Capacity, ownership, replenishment, and commercial terms are unspecified.",
            ),
            variable(
                "usual-quote-turnaround",
                "Usual quote turnaround",
                "service-expectation",
                quantity("24", "h"),
                certainty="typical",
                notes="Often same day. No guarantee, business calendar, or start event was specified. "
                "This records a shop characteristic only.",
            ),
            variable(
                "annual-capacity",
                "Annual production capacity",
                "production-planning",
                None,
                certainty="unknown",
            ),
        ),
    )
