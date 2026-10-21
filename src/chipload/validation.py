"""Cross-object validation with locations agents can use to repair a project."""

from __future__ import annotations

from collections import Counter
from typing import Annotated

from pydantic import Field, StrictStr

from .complexity import complexity_errors
from .graph import DependencyCycle, ordered_nodes
from .limits import MAX_DIAGNOSTICS
from .listing_validation import listing_errors
from .model import Choice, Fact, Id, Model, Rule, Shop


class Diagnostic(Model):
    code: Id
    path: tuple[Annotated[StrictStr, Field(max_length=200)], ...] = Field(max_length=32)
    message: Annotated[StrictStr, Field(max_length=1000)]


class ShopValidationError(ValueError):
    def __init__(self, diagnostics: list[Diagnostic]):
        self.diagnostics = tuple(diagnostics)
        super().__init__("\n".join(f"{'/'.join(d.path)}: {d.message}" for d in diagnostics))


def validate_shop(shop: Shop) -> None:
    """Reject broken identities, graph relationships, and ambiguous definitions."""
    errors: list[Diagnostic] = []

    def error(code: str, path: tuple[str, ...], message: str) -> None:
        errors.append(Diagnostic(code=code, path=path, message=message[:1000]))
        if len(errors) == MAX_DIAGNOSTICS:
            raise ShopValidationError(errors)

    def unique(values: list[str] | tuple[str, ...], path: tuple[str, ...]) -> None:
        for value, count in Counter(values).items():
            if count > 1:
                error("duplicate-id", path + (value,), "Identifier appears more than once.")

    def ref(value: str, collection: str, path: tuple[str, ...]) -> None:
        if value not in indexes[collection]:
            error("missing-reference", path, f"Unknown {collection} reference: {value}.")

    def facts(values: tuple[Fact, ...], path: tuple[str, ...]) -> None:
        unique([f.name for f in values], path)

    def rules(values: tuple[Rule, ...], path: tuple[str, ...]) -> None:
        unique([rule.fact for rule in values], path)
        for rule in values:
            if isinstance(rule, Choice) and rule.catalog is not None:
                ref(rule.catalog, "catalogs", path + (rule.fact, "catalog"))
                if rule.catalog in catalog_items:
                    items = catalog_items[rule.catalog]
                    for value in rule.allowed:
                        if value not in items:
                            error(
                                "missing-reference",
                                path + (rule.fact, "allowed"),
                                "Unknown catalog item.",
                            )

    def acyclic(graph: dict[str, tuple[str, ...]], path: tuple[str, ...]) -> None:
        try:
            ordered_nodes(graph)
        except DependencyCycle as cycle:
            error("dependency-cycle", path, "Cycle blocks: " + ", ".join(cycle.nodes[:10]))

    collections = (
        "sites",
        "resources",
        "catalogs",
        "processes",
        "capabilities",
        "workflows",
        "offers",
        "evidence",
        "variables",
        "operations",
        "listings",
    )
    indexes = {}
    for collection in collections:
        items = getattr(shop, collection)
        unique([item.id for item in items], (collection,))
        indexes[collection] = {item.id: item for item in items}
    catalog_items = {catalog.id: {item.id for item in catalog.items} for catalog in shop.catalogs}
    for catalog in shop.catalogs:
        unique([item.id for item in catalog.items], ("catalogs", catalog.id, "items"))
        for item in catalog.items:
            facts(item.properties, ("catalogs", catalog.id, "items", item.id, "properties"))
    for variable in shop.variables:
        rules(variable.applies_when, ("variables", variable.id, "applies_when"))
    for offer in shop.offers:
        path = ("offers", offer.id)
        rules(offer.requirements, path + ("requirements",))
        for index, route in enumerate(offer.alternatives):
            ref(route.target, "capabilities" if route.target_kind == "capability" else "workflows",
                path + ("alternatives", str(index), "target"))
    for operation in shop.operations:
        path = ("operations", operation.id)
        unique(operation.resources, path + ("resources",))
        for resource in operation.resources:
            ref(resource, "resources", path + ("resources", resource))
        for name in ("inputs", "outputs"):
            unique([item.name for item in getattr(operation, name)], path + (name,))
        inputs = {item.name for item in operation.inputs}
        for index, check in enumerate(operation.checks):
            check_path = path + ("checks", str(index))
            ref(
                check.target,
                "capabilities" if check.target_kind == "capability" else "workflows",
                check_path + ("target",),
            )
            unique(check.inputs, check_path + ("inputs",))
            facts(check.initial_state, check_path + ("initial_state",))
            for name in check.inputs:
                if name not in inputs:
                    error(
                        "missing-reference",
                        check_path + ("inputs", name),
                        "Check input must name an operation input.",
                    )
    for resource in shop.resources:
        path = ("resources", resource.id)
        if resource.site is not None:
            ref(resource.site, "sites", path + ("site",))
        facts(resource.private_details, path + ("private_details",))
        if resource.parent:
            ref(resource.parent, "resources", path + ("parent",))
            parent = indexes["resources"].get(resource.parent)
            if (
                parent
                and parent.site is not None
                and resource.site is not None
                and parent.site != resource.site
            ):
                error("cross-site-parent", path + ("parent",), "Parent must be at the same site.")
    acyclic({r.id: (r.parent,) if r.parent else () for r in shop.resources}, ("resources",))

    for capability in shop.capabilities:
        path = ("capabilities", capability.id)
        ref(capability.process, "processes", path + ("process",))
        for collection in ("resources", "evidence"):
            values = getattr(capability, collection)
            unique(values, path + (collection,))
            for value in values:
                ref(value, collection, path + (collection, value))
        rules(capability.requirements, path + ("requirements",))
        unique([check.id for check in capability.python_checks], path + ("python_checks",))
        inputs = {rule.fact for rule in capability.requirements}
        for check in capability.python_checks:
            for name in check.facts:
                if name not in inputs:
                    error("missing-reference", path + ("python_checks", check.id, "facts", name),
                          "Python check inputs must name this capability's declared requirements.")
        facts(capability.input_state, path + ("input_state",))
        facts(capability.output_state, path + ("output_state",))
        unique([o.observation for o in capability.observations], path + ("observations",))
        for requirement in capability.observations:
            ref(requirement.resource, "resources", path + ("observations", requirement.observation))
            if requirement.resource not in capability.resources:
                error(
                    "unbound-observation",
                    path + ("observations", requirement.observation),
                    "Observation must concern one of this capability's resources.",
                )
        for evidence_id in capability.evidence:
            evidence = indexes["evidence"].get(evidence_id)
            if evidence and capability.id not in evidence.capabilities:
                error(
                    "evidence-scope",
                    path + ("evidence", evidence_id),
                    "Evidence does not name this capability.",
                )

    for evidence in shop.evidence:
        path = ("evidence", evidence.id)
        for collection in ("capabilities", "resources"):
            values = getattr(evidence, collection)
            unique(values, path + (collection,))
            for value in values:
                ref(value, collection, path + (collection, value))
        rules(evidence.requirements, path + ("requirements",))
        for rule in evidence.requirements:
            if rule.on_mismatch != "unsupported" or rule.on_missing != "unknown":
                error(
                    "conditional-evidence",
                    path + ("requirements", rule.fact),
                    "Evidence scope cannot be conditional.",
                )

    for workflow in shop.workflows:
        path = ("workflows", workflow.id)
        rules(workflow.requirements, path + ("requirements",))
        unique([step.id for step in workflow.steps], path + ("steps",))
        if not workflow.steps:
            error("missing-steps", path + ("steps",), "Workflow needs at least one step.")
        steps = {step.id for step in workflow.steps}
        for step in workflow.steps:
            ref(step.capability, "capabilities", path + ("steps", step.id, "capability"))
            unique(step.after, path + ("steps", step.id, "after"))
            for predecessor in step.after:
                if predecessor not in steps:
                    error(
                        "missing-reference",
                        path + ("steps", step.id, "after"),
                        f"Unknown predecessor: {predecessor}.",
                    )
        acyclic({step.id: step.after for step in workflow.steps}, path + ("steps",))
    for operation in shop.operations:
        required = set()
        for check in operation.checks:
            if check.target_kind == "capability":
                capabilities = [indexes["capabilities"].get(check.target)]
            else:
                workflow = indexes["workflows"].get(check.target)
                capabilities = (
                    [indexes["capabilities"].get(step.capability) for step in workflow.steps]
                    if workflow
                    else []
                )
            for capability in capabilities:
                if capability:
                    required.update(capability.resources)
        if not required.issubset(operation.resources):
            error(
                "unbound-operation-resource",
                ("operations", operation.id, "resources"),
                "Operation resources must include every resource used by its checks.",
            )
    if errors:
        raise ShopValidationError(errors)
    for code, path, message in listing_errors(shop):
        error(code, path, message)
    if errors:
        raise ShopValidationError(errors)
    for path, message in complexity_errors(shop):
        error("model-complexity", path, message)
    if errors:
        raise ShopValidationError(errors)
