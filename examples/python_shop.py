"""Shop-defined Python constraints with technical relationships and business rules."""

from datetime import datetime, timezone
from pathlib import Path

from chipload import (
    BuildIdentity,
    Capability,
    Choice,
    Evidence,
    Fact,
    Process,
    PythonCheck,
    Quantity,
    Range,
    Resource,
    Shop,
    Step,
    Workflow,
    compile_shop,
    quantity,
)
from chipload.evaluate import EvaluationRequest
from chipload.project import AcceptanceCase, SourceFile, SourceProject, run_cases

AT = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
PROFILES = {
    "machining": ("depth", "mm", "diameter", "mm", "20", "10", "mill", "prototype", "production"),
    "thermal": ("hold", "min", "mass", "kg", "30", "10", "furnace", "trial", "contract"),
    "review": ("pages", "1", "reviewers", "1", "40", "2", None, "one-off", "retainer"),
}


def measurements(facts, left, right, dimensions):
    a, b = facts[left], facts[right]
    if not isinstance(a, Quantity) or not isinstance(b, Quantity):
        return None
    if (a.dimension, b.dimension) != dimensions:
        return None
    return a.base_value(), b.base_value()


def machining_fit(facts):
    values = measurements(facts, "depth", "diameter", ("length", "length"))
    if values is None:
        return "unknown"
    depth, diameter = values
    return "supported" if depth <= 3 * diameter else "unsupported"


def thermal_fit(facts):
    values = measurements(facts, "hold", "mass", ("time", "mass"))
    if values is None:
        return "unknown"
    duration, mass = values
    # Fictional declared relationship, not a qualified heat-treatment recipe.
    minimum = quantity("2", "min").base_value() * mass / quantity("1", "kg").base_value()
    return "supported" if duration >= minimum else "unsupported"


def review_fit(facts):
    values = measurements(facts, "pages", "reviewers", ("ratio", "ratio"))
    if values is None:
        return "unknown"
    pages, reviewers = values
    return "supported" if pages <= 50 * reviewers else "unsupported"


def business(facts, small, contracted):
    amount = facts["quantity"]
    mode = facts["engagement"]
    ready = facts["documentation-ready"]
    if (
        not isinstance(amount, Quantity)
        or amount.dimension != "ratio"
        or mode is None
        or ready is None
    ):
        return "unknown"
    if mode == small and amount.base_value() > 20:
        return "unsupported"
    if mode == contracted and amount.base_value() < 100:
        return "unsupported"
    return "supported" if ready else "conditional"


def machining_business(facts):
    return business(facts, "prototype", "production")


def thermal_business(facts):
    return business(facts, "trial", "contract")


def review_business(facts):
    return business(facts, "one-off", "retainer")


def define_shop(profile="machining"):
    first, first_unit, second, second_unit, _, _, resource, small, contracted = PROFILES[profile]
    requirements = (
        Range(fact=first, minimum=quantity("1", first_unit), maximum=quantity("1000", first_unit)),
        Range(
            fact=second, minimum=quantity("1", second_unit), maximum=quantity("1000", second_unit)
        ),
        Range(
            fact="quantity",
            minimum=quantity("1", "1"),
            maximum=quantity("10000", "1"),
            multiple_of=quantity("1", "1"),
        ),
        Choice(fact="engagement", allowed=(small, contracted)),
        Choice(fact="documentation-ready", allowed=(True, False)),
    )
    return Shop(
        id="python-" + profile,
        name="Python " + profile,
        visibility="public",
        processes=(Process(id="provide", name="Provide service", visibility="public"),),
        resources=(Resource(id="resource", name="Example resource", kind=resource),)
        if resource
        else (),
        capabilities=(
            Capability(
                id="service",
                name="Service",
                visibility="public",
                process="provide",
                resources=("resource",) if resource else (),
                requirements=requirements,
                evidence=("declaration",),
                python_checks=(
                    PythonCheck(
                        id="relationship",
                        name="Input relationship",
                        visibility="public",
                        entrypoint=f"shop:{profile}_fit",
                        facts=(first, second),
                    ),
                    PythonCheck(
                        id="business",
                        name="Business conditions",
                        visibility="public",
                        entrypoint=f"shop:{profile}_business",
                        facts=("engagement", "quantity", "documentation-ready"),
                        remedy="Confirm the required input documentation before proceeding.",
                    ),
                ),
            ),
        ),
        evidence=(
            Evidence(
                id="declaration",
                name="Simulated declaration",
                level="declared",
                source="simulated",
                claim="Fictional example scope",
                recorded_at=AT,
                capabilities=("service",),
                resources=("resource",) if resource else (),
                requirements=requirements,
            ),
        ),
        workflows=(
            Workflow(
                id="service-route",
                name="Service route",
                visibility="public",
                steps=(Step(id="provide", capability="service"),),
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
    first, first_unit, second, second_unit, a, b, _, small, contracted = PROFILES[profile]
    base = {
        first: quantity(a, first_unit),
        second: quantity(b, second_unit),
        "quantity": quantity("1", "1"),
        "engagement": small,
        "documentation-ready": True,
    }
    scenarios = (
        ("supported", {}, "supported"),
        (
            "relationship-rejected",
            {first: quantity("1" if profile == "thermal" else "200", first_unit)},
            "unsupported",
        ),
        ("contracted", {"engagement": contracted, "quantity": quantity("100", "1")}, "supported"),
        ("contract-too-small", {"engagement": contracted}, "unsupported"),
        ("small-lot-too-large", {"quantity": quantity("21", "1")}, "unsupported"),
        ("documentation-needed", {"documentation-ready": False}, "conditional"),
        ("missing-technical", {first: None}, "unknown"),
        ("missing-business", {"engagement": None}, "unknown"),
        (
            "static-limit",
            {"engagement": contracted, "quantity": quantity("10001", "1")},
            "unsupported",
        ),
        ("qualification-missing", {}, "unknown"),
    )
    return tuple(
        AcceptanceCase(
            id=name,
            at=AT,
            expected=expected,
            request=EvaluationRequest(
                target_kind="workflow",
                target="service-route",
                allow_simulated=True,
                minimum_evidence="qualified" if name == "qualification-missing" else "declared",
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
        shop_id="python-" + profile,
        entrypoint="shop:define_" + profile,
        files=(SourceFile(path="shop.py", content=Path(__file__).read_text()),),
        tests=cases(profile),
    )


if __name__ == "__main__":
    # This command runs only this trusted example locally. Public services never
    # invoke these functions; isolated builds execute their exported source.
    import sys

    from chipload.check_worker import run_checks
    from chipload.python_checks import bind_case_results

    sys.modules["shop"] = sys.modules[__name__]
    for profile in PROFILES:
        source = project(profile)
        shop = define_shop(profile)
        artifact = compile_shop(
            shop,
            BuildIdentity(
                source_sha256=source.revision,
                sdk_sha256="a" * 64,
                dependencies_sha256="b" * 64,
                runtime="trusted-example",
            ),
        )
        checks = bind_case_results(artifact, source.tests, run_checks(shop, source))
        tests = run_cases(artifact, source.tests, python_evaluations=checks)
        assert all(case.passed for case in tests), tests
        print(f"{profile}: {len(tests)} Python technical/business cases passed")
