"""Listing references and public input mappings, independent of shop vocabulary."""

from functools import lru_cache

from .model import Choice


def listing_errors(shop):
    """Run after the existing target graphs have been validated."""
    targets = {
        "capability": {item.id: item for item in shop.capabilities},
        "workflow": {item.id: item for item in shop.workflows},
        "offer": {item.id: item for item in shop.offers},
    }
    sites = {item.id: item for item in shop.sites}
    catalogs = {item.id: item for item in shop.catalogs}
    items = {key: {item.id: item for item in catalog.items} for key, catalog in catalogs.items()}

    @lru_cache(maxsize=6000)
    def inputs(kind, identifier, public):
        target = targets[kind][identifier]
        result = {}
        if public and target.visibility != "public":
            return result
        for rule in target.requirements:
            result.setdefault(("facts", rule.fact), set())
            if isinstance(rule, Choice) and rule.catalog:
                result["facts", rule.fact].add(rule.catalog)
        if kind == "capability":
            for fact in target.input_state:
                result.setdefault(("initial_state", fact.name), set())
            routes = ()
        elif kind == "workflow":
            routes = (("capability", step.capability) for step in target.steps)
        else:
            routes = ((route.target_kind, route.target) for route in target.alternatives)
        for child_kind, child in routes:
            for key, references in inputs(child_kind, child, public).items():
                result.setdefault(key, set()).update(references)
        return result

    for listing in shop.listings:
        path = ("listings", listing.id)
        public = listing.visibility == "public"
        target = targets[listing.target_kind].get(listing.target)
        if target is None:
            yield "missing-reference", path + ("target",), "Unknown listing target."
            continue
        if public and target.visibility != "public":
            yield "private-listing-target", path + ("target",), "Public listings require a public target."
        if listing.indexed and shop.visibility != "public":
            yield "private-indexed-shop", path, "Indexing requires a public shop."
        if listing.site is not None:
            site = sites.get(listing.site)
            if site is None:
                yield "missing-reference", path + ("site",), "Unknown listing site."
            elif public and site.visibility != "public":
                yield "private-listing-site", path + ("site",), "Public listings require a public site."
        declared = inputs(listing.target_kind, listing.target, public)
        for index, field in enumerate(listing.fields):
            field_path = path + ("fields", str(index))
            references = declared.get((field.context, field.fact))
            if references is None:
                yield "missing-reference", field_path, "Input must bind a declared target input with matching visibility."
                continue
            # Mappings are part of the published contract. They cannot disclose
            # a hidden catalog or its item values through a second projection.
            for choice in field.choices:
                for catalog_id in references:
                    item = items[catalog_id].get(choice.value) if isinstance(choice.value, str) else None
                    if item is None:
                        yield "missing-reference", field_path, "Mapped value must name an item in its input catalog."
                    elif public and (catalogs[catalog_id].visibility != "public" or item.visibility != "public"):
                        yield "private-listing-value", field_path, "Public mappings require public catalog items."
