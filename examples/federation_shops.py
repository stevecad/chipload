"""Fictional supplier listings with exact input meanings and business conditions."""

from datetime import datetime, timezone
from pathlib import Path

from chipload import (
    BuildIdentity,
    Capability,
    Catalog,
    CatalogItem,
    Choice,
    EvaluationRequest,
    Evidence,
    Fact,
    GeoPoint,
    Listing,
    ListingChoice,
    ListingField,
    ObservationRequirement,
    Process,
    Range,
    Resource,
    SemanticRef,
    Shop,
    Site,
    Step,
    Workflow,
    compile_shop,
    quantity,
)
from chipload.discovery import public_catalog
from chipload.listings import ListingInput, ListingQuery
from chipload.project import AcceptanceCase, SourceFile, SourceProject, run_cases

AT = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)


def term(name):
    # Example vocabulary only. No term in this module is an engine default.
    return SemanticRef(namespace="examples.chipload.org", version="1", term=name)


PROFILES = {
    "supplier-near": dict(profile="material-supply", name="Nearby material supplier",
                          inputs={"material-grade": "aluminum-6061", "condition": "t6", "form": "plate"},
                          measure="thickness", unit="mm", maximum="100", minimum_quantity="100",
                          longitude="-87.65"),
    "supplier-far": dict(profile="material-supply", name="Small-lot material supplier",
                         inputs={"material-grade": "aluminum-6061", "condition": "t6", "form": "plate"},
                         measure="thickness", unit="mm", maximum="100", minimum_quantity="1",
                         longitude="-87.85"),
    "mill": dict(profile="milling-service", name="Milling service",
                 inputs={"material-grade": "aluminum-6061", "condition": "t6", "form": "plate"},
                 measure="part-width", unit="mm", maximum="200", minimum_quantity="1",
                 longitude="-87.7"),
    "thermal": dict(profile="heat-treatment-service", name="Heat-treatment service",
                    inputs={"material-grade": "steel-4140", "condition": "annealed", "form": "bar"},
                    measure="temperature", unit="Cel", maximum="900", minimum_quantity="10",
                    longitude="-87.75"),
    "review": dict(profile="document-review", name="Document review service",
                   inputs={"document-format": "pdf"}, measure="page-count", unit="1",
                   maximum="1000", minimum_quantity="1", longitude=None),
}


def define_shop(profile="supplier-far"):
    config = PROFILES[profile]
    technical = tuple(Choice(fact=key, allowed=(value,), catalog=key)
                      for key, value in config["inputs"].items()) + (
        Range(fact=config["measure"], minimum=quantity("1", config["unit"]),
              maximum=quantity(config["maximum"], config["unit"])),
    )
    physical = config["longitude"] is not None
    return Shop(
        id=profile, name=config["name"], visibility="public",
        sites=(Site(id="site", name="Service location", visibility="public",
                    public_location=GeoPoint(latitude="41.88", longitude=config["longitude"], accuracy_m=100)),) if physical else (),
        resources=(Resource(id="availability", name="External availability record",
                            kind="external-observation", site="site"),) if physical else (),
        catalogs=tuple(Catalog(id=key, name=key, visibility="public", items=(
            CatalogItem(id=value, name=value, visibility="public"),
        )) for key, value in config["inputs"].items()),
        processes=(Process(id="process", name=config["profile"], visibility="public"),),
        capabilities=(Capability(
            id="technical", name="Technical scope", visibility="public", process="process",
            resources=("availability",) if physical else (), requirements=technical,
            evidence=("declaration",),
            observations=(ObservationRequirement(
                observation="availability", resource="availability", property="available",
                allowed=(True,), purpose="readiness", remedy="Confirm current external availability.",
            ),) if physical else (),
        ),),
        evidence=(Evidence(id="declaration", name="Simulated declaration", level="declared",
                           source="simulated", claim="Example input scope", capabilities=("technical",),
                           resources=("availability",) if physical else (), requirements=technical,
                           recorded_at=AT),),
        workflows=(Workflow(id="offering", name="Technical and business acceptance", visibility="public",
                            requirements=(
                                Range(fact="quantity", minimum=quantity(config["minimum_quantity"], "1"),
                                      maximum=quantity("10000", "1"), multiple_of=quantity("1", "1")),
                                Choice(fact="documentation", allowed=("complete",), on_mismatch="conditional",
                                       remedy="Provide the required material or input documentation."),
                            ), steps=(Step(id="process", capability="technical"),)),),
        listings=(Listing(
            id="service", name=config["name"], visibility="public", indexed=True,
            profile=term(config["profile"]), target_kind="workflow", target="offering",
            site="site" if physical else None,
            fields=tuple(ListingField(term=term(key), fact=key,
                                      choices=(ListingChoice(term=term(value), value=value),))
                         for key, value in config["inputs"].items()) + (
                ListingField(term=term(config["measure"]), fact=config["measure"]),
                ListingField(term=term("quantity"), fact="quantity"),
                ListingField(term=term("documentation"), fact="documentation"),
            ),
        ),),
    )


def query(profile="supplier-far", *, revision, count="20", documentation="complete", purpose="capability"):
    config = PROFILES[profile]
    return ListingQuery(
        profile=term(config["profile"]), expected_revision=revision, allow_simulated=True, purpose=purpose,
        inputs=tuple(ListingInput(term=term(key), value=term(value)) for key, value in config["inputs"].items()) + (
            ListingInput(term=term(config["measure"]), value=quantity("10", config["unit"])),
            ListingInput(term=term("quantity"), value=quantity(count, "1")),
            ListingInput(term=term("documentation"), value=documentation),
        ),
    )


def cases(profile):
    config = PROFILES[profile]
    facts = tuple(Fact(name=key, value=value) for key, value in config["inputs"].items()) + (
        Fact(name=config["measure"], value=quantity("10", config["unit"])),
        Fact(name="quantity", value=quantity(config["minimum_quantity"], "1")),
        Fact(name="documentation", value="complete"),
    )
    base = EvaluationRequest(target_kind="workflow", target="offering", facts=facts, allow_simulated=True)
    return (
        AcceptanceCase(id="accepted", request=base, at=AT, expected="supported"),
        AcceptanceCase(id="quantity-refused", request=base.model_copy(update={"facts": (
            *facts[:-2], Fact(name="quantity", value=quantity("0", "1")), facts[-1],
        )}), at=AT, expected="unsupported"),
        AcceptanceCase(id="documentation-conditional", request=base.model_copy(update={"facts": (
            *facts[:-1], Fact(name="documentation", value="incomplete"),
        )}), at=AT, expected="conditional"),
        AcceptanceCase(id="missing-documentation", request=base.model_copy(update={"facts": facts[:-1]}),
                       at=AT, expected="unknown"),
    )


def project(profile="supplier-far"):
    # One ordinary source file plus a tiny entrypoint; the worker imports no examples package.
    return SourceProject(shop_id=profile, entrypoint="shop:define_shop", tests=cases(profile), files=(
        SourceFile(path="definition.py", content=Path(__file__).read_text()),
        SourceFile(path="shop.py", content=f"from definition import define_shop as build\ndef define_shop():\n    return build({profile!r})\n"),
    ))


if __name__ == "__main__":
    for profile in PROFILES:
        source = project(profile)
        artifact = compile_shop(define_shop(profile), BuildIdentity(
            source_sha256=source.revision, sdk_sha256="a" * 64,
            dependencies_sha256="b" * 64, runtime="trusted-local-example",
        ))
        results = run_cases(artifact, source.tests)
        if not all(result.passed for result in results):
            raise SystemExit(f"{profile}: failed acceptance cases")
        print(f"{profile}: {len(results)} cases passed; {len(public_catalog(artifact)['listings'])} listing")
