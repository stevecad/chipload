"""Explicit public projection. Internal models are never public response DTOs."""

from .artifact import Artifact, canonical_json, digest
from .model import Choice, Entity, Rule


def public_catalog(artifact: Artifact) -> dict:
    """Return only opted-in public metadata and public reference IDs.

    This is the catalog projection, not publication authorization. A service
    must select a published artifact before calling it.
    """
    shop = artifact.shop
    if shop.visibility != "public":
        raise KeyError("shop is not public")

    def visible(items: tuple) -> set[str]:
        return {item.id for item in items if item.visibility == "public"}

    def summary(entity: Entity) -> dict:
        return {"id": entity.id, "name": entity.name, "description": entity.description}

    public_sites = visible(shop.sites)
    public_resources = visible(shop.resources)
    public_catalogs = visible(shop.catalogs)
    public_items = {
        catalog.id: visible(catalog.items)
        for catalog in shop.catalogs
        if catalog.id in public_catalogs
    }
    public_processes = visible(shop.processes)
    public_capabilities = visible(shop.capabilities)

    def public_rule(rule: Rule) -> dict:
        result = rule.model_dump(mode="json")
        if isinstance(rule, Choice) and rule.catalog is not None:
            result["catalog"] = rule.catalog if rule.catalog in public_catalogs else None
            result["allowed"] = [
                value for value in rule.allowed if value in public_items.get(rule.catalog, set())
            ]
            result["complete"] = len(result["allowed"]) == len(rule.allowed)
        return result

    catalog = {
        "schema_version": shop.schema_version,
        "shop": summary(shop),
        "sites": [
            {**summary(s), "time_zone": s.time_zone} for s in shop.sites if s.id in public_sites
        ],
        "resources": [
            {
                **summary(r),
                "kind": r.kind,
                "status": r.status,
                "site": r.site if r.site in public_sites else None,
                "parent": r.parent if r.parent in public_resources else None,
            }
            for r in shop.resources
            if r.id in public_resources
        ],
        "catalogs": [
            {
                **summary(catalog),
                "items": [
                    {
                        **summary(item),
                        "properties": [f.model_dump(mode="json") for f in item.properties],
                    }
                    for item in catalog.items
                    if item.id in public_items[catalog.id]
                ],
            }
            for catalog in shop.catalogs
            if catalog.id in public_catalogs
        ],
        "processes": [
            {**summary(p), "delivery": p.delivery}
            for p in shop.processes
            if p.id in public_processes
        ],
        "capabilities": [
            {
                **summary(c),
                "process": c.process if c.process in public_processes else None,
                "resources": [r for r in c.resources if r in public_resources],
                # Constraints are deliberately part of a published capability's
                # contract; authoring/publish review must make that explicit.
                "requirements": [public_rule(r) for r in c.requirements],
                "input_state": [f.model_dump(mode="json") for f in c.input_state],
                "output_state": [f.model_dump(mode="json") for f in c.output_state],
            }
            for c in shop.capabilities
            if c.id in public_capabilities
        ],
        # Step internals, source, evidence references, observation identifiers,
        # equipment details, and build provenance need separate authorized views.
        "workflows": [
            {
                **summary(w),
                "requirements": [public_rule(r) for r in w.requirements],
                "capabilities": sorted(
                    {s.capability for s in w.steps if s.capability in public_capabilities}
                ),
                "complete": all(s.capability in public_capabilities for s in w.steps),
            }
            for w in shop.workflows
            if w.visibility == "public"
        ],
        "variables": [
            {
                **variable.model_dump(mode="json", exclude={"visibility", "applies_when"}),
                "applies_when": [public_rule(r) for r in variable.applies_when],
            }
            for variable in shop.variables
            if variable.visibility == "public"
        ],
        "operations": [
            {
                **summary(operation),
                "effect": operation.effect,
                "effect_description": operation.effect_description,
                "inputs": [field.model_dump(mode="json") for field in operation.inputs],
                "outputs": [field.model_dump(mode="json") for field in operation.outputs],
                "timeout_seconds": operation.timeout_seconds,
            }
            for operation in shop.operations
            if operation.visibility == "public"
        ],
    }
    if shop.offers:
        workflows = {item["id"]: item for item in catalog["workflows"]}
        catalog["offers"] = [
            {
                **summary(offer),
                "requirements": [public_rule(rule) for rule in offer.requirements],
                "alternatives": [
                    route.model_dump(mode="json") for route in offer.alternatives
                    if (route.target_kind == "capability" and route.target in public_capabilities)
                    or (route.target_kind == "workflow" and route.target in workflows)
                ],
                "complete": all(
                    route.target in public_capabilities if route.target_kind == "capability"
                    else route.target in workflows and workflows[route.target]["complete"]
                    for route in offer.alternatives
                ),
            }
            for offer in shop.offers if offer.visibility == "public"
        ]
        # Private offers must not change public cache identities.
        if not catalog["offers"]:
            del catalog["offers"]
    # Public cache identities must not fingerprint private equipment details.
    return {"public_sha256": digest(canonical_json(catalog)), **catalog}
