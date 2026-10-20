import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from chipload import (
    BuildIdentity,
    Capability,
    Catalog,
    CatalogItem,
    Choice,
    EvaluationRequest,
    Evidence,
    Fact,
    Process,
    Resource,
    Shop,
    ShopValidationError,
    compile_shop,
    evaluate,
    quantity,
    read_artifact,
)
from chipload.discovery import public_catalog
from chipload.evaluate import same
from examples import heat_treatment

AT = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
IDENTITY = BuildIdentity(
    source_sha256="a" * 64, sdk_sha256="b" * 64, dependencies_sha256="c" * 64, runtime="unit-test"
)


def document_shop() -> Shop:
    """A service with no physical material, machine, dimensional envelope, or site."""
    rule = Choice(fact="document-format", allowed=("pdf",), catalog="document-formats")
    return Shop(
        id="document-review",
        name="Document Review",
        visibility="public",
        resources=(Resource(id="review-team", name="Review team", kind="qualified-review-team"),),
        processes=(Process(id="document-check", name="Document checking"),),
        catalogs=(
            Catalog(
                id="document-formats",
                name="Formats",
                visibility="public",
                items=(CatalogItem(id="pdf", name="PDF", visibility="public"),),
            ),
        ),
        capabilities=(
            Capability(
                id="check-record",
                name="Check record",
                process="document-check",
                visibility="public",
                resources=("review-team",),
                requirements=(rule,),
                evidence=("record-declaration",),
            ),
        ),
        evidence=(
            Evidence(
                id="record-declaration",
                name="Declared scope",
                level="declared",
                source="simulated",
                recorded_at=AT,
                claim="Synthetic test service.",
                capabilities=("check-record",),
                resources=("review-team",),
                requirements=(rule,),
            ),
        ),
    )


class AbstractEngineTests(unittest.TestCase):
    def test_non_material_service_uses_the_same_engine(self):
        artifact = compile_shop(document_shop(), IDENTITY)
        query = EvaluationRequest(
            target_kind="capability",
            target="check-record",
            facts=(Fact(name="document-format", value="pdf"),),
            allow_simulated=True,
        )
        self.assertEqual(evaluate(artifact, query, at=AT).outcome, "supported")
        query = query.model_copy(update={"facts": (Fact(name="document-format", value="unknown"),)})
        self.assertEqual(evaluate(artifact, query, at=AT).outcome, "unsupported")
        self.assertEqual(read_artifact(artifact.to_json()), artifact)
        self.assertEqual(artifact.shop.resources[0].kind, "qualified-review-team")
        self.assertIsNone(artifact.shop.resources[0].site)

    def test_heat_treatment_only_shop_acceptance_scenarios(self):
        artifact = compile_shop(heat_treatment.define_shop(), IDENTITY)
        for scenario in heat_treatment.SCENARIOS:
            with self.subTest(scenario=scenario.name):
                result = evaluate(
                    artifact,
                    scenario.question,
                    at=heat_treatment.AT,
                    observations=heat_treatment.observations(),
                )
                self.assertEqual(result.outcome, scenario.expected)
                self.assertIn(scenario.reason, {r.code for r in result.reasons})
        self.assertEqual(read_artifact(artifact.to_json()), artifact)

    def test_temperature_conversions_include_offsets(self):
        for left, right in (
            (quantity("0", "Cel"), quantity("273.15", "K")),
            (quantity("32", "[degF]"), quantity("0", "Cel")),
            (quantity("212", "[degF]"), quantity("100", "Cel")),
            (quantity("-40", "[degF]"), quantity("-40", "Cel")),
        ):
            self.assertTrue(same(left, right))
        self.assertFalse(same(quantity("0", "Cel"), quantity("0", "K")))
        artifact = compile_shop(heat_treatment.define_shop(), IDENTITY)
        query = heat_treatment.request(
            changes={"cycle-temperature": quantity("1292.000001", "[degF]")}
        )
        self.assertEqual(
            evaluate(artifact, query, at=AT, observations=heat_treatment.observations()).outcome,
            "unsupported",
        )

    def test_every_quantity_requires_an_explicit_unit(self):
        with self.assertRaises(TypeError):
            quantity("1")

    def test_unknown_catalogs_and_items_produce_structured_errors(self):
        for catalog, allowed in (("not-present", ("pdf",)), ("document-formats", ("not-present",))):
            shop = document_shop()
            capability = shop.capabilities[0].model_copy(
                update={
                    "requirements": (
                        Choice(fact="document-format", catalog=catalog, allowed=allowed),
                    )
                }
            )
            with self.assertRaises(ShopValidationError) as caught:
                compile_shop(shop.model_copy(update={"capabilities": (capability,)}), IDENTITY)
            self.assertIn("missing-reference", {d.code for d in caught.exception.diagnostics})

    def test_private_catalog_items_are_not_disclosed_by_public_requirements(self):
        shop = document_shop()
        catalog = shop.catalogs[0]
        private_item = CatalogItem(id="secret-format", name="Private customer format")
        catalog = catalog.model_copy(update={"items": (*catalog.items, private_item)})
        capability = shop.capabilities[0]
        rule = Choice(fact="document-format", catalog=catalog.id, allowed=("pdf", "secret-format"))
        capability = capability.model_copy(update={"requirements": (rule,)})
        artifact = compile_shop(
            shop.model_copy(update={"catalogs": (catalog,), "capabilities": (capability,)}),
            IDENTITY,
        )
        result = public_catalog(artifact)
        self.assertNotIn("secret-format", str(result))
        published_rule = result["capabilities"][0]["requirements"][0]
        self.assertEqual(published_rule["allowed"], ["pdf"])
        self.assertFalse(published_rule["complete"])

    def test_unrecognized_input_is_not_silently_accepted_by_a_generic_service(self):
        artifact = compile_shop(document_shop(), IDENTITY)
        result = evaluate(
            artifact,
            EvaluationRequest(
                target_kind="capability",
                target="check-record",
                allow_simulated=True,
                facts=(Fact(name="document-format", value="pdf"), Fact(name="signed", value=True)),
            ),
            at=AT,
        )
        self.assertEqual(result.outcome, "unknown")
        self.assertIn("unmodeled-requirement", {reason.code for reason in result.reasons})

    def test_evidence_from_a_different_catalog_cannot_prove_support(self):
        shop = document_shop()
        unrelated = shop.catalogs[0].model_copy(update={"id": "unrelated"})
        proof = shop.evidence[0].model_copy(
            update={
                "requirements": (
                    Choice(fact="document-format", allowed=("pdf",), catalog="unrelated"),
                )
            }
        )
        artifact = compile_shop(
            shop.model_copy(
                update={
                    "catalogs": (*shop.catalogs, unrelated),
                    "evidence": (proof,),
                }
            ),
            IDENTITY,
        )
        result = evaluate(
            artifact,
            EvaluationRequest(
                target_kind="capability",
                target="check-record",
                allow_simulated=True,
                facts=(Fact(name="document-format", value="pdf"),),
            ),
            at=AT,
        )
        self.assertEqual(result.outcome, "unknown")

    def test_resource_taxonomy_is_open_but_identifiers_are_validated(self):
        for kind in ("furnace", "plating-bath", "inspection-lab", "human-team", "external-service"):
            self.assertEqual(Resource(id="resource", name="Resource", kind=kind).kind, kind)
        with self.assertRaises(ValidationError):
            Resource(id="resource", name="Resource", kind="not a valid identifier")


if __name__ == "__main__":
    unittest.main()
