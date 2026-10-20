"""Process-specific offers, business conditions, and mixed-shop composition."""

import unittest

from pydantic import ValidationError

from chipload import (
    BuildIdentity,
    Fact,
    Range,
    VariableQuery,
    compile_shop,
    evaluate,
    quantity,
    query_variables,
    read_artifact,
)
from chipload.artifact import canonical_json, digest
from chipload.discovery import public_catalog
from chipload.evaluate import check_rule
from chipload.project import run_cases
from examples.process_shops import mixed
from examples.process_shops.business import TERMS
from examples.process_shops.definitions import AT
from examples.process_shops.export import project
from examples.process_shops.library import BY_ID, EXAMPLES
from examples.process_shops.scenarios import request, scenarios
from examples.process_shops.shops import define_shop

IDENTITY = BuildIdentity(
    source_sha256="a" * 64,
    sdk_sha256="b" * 64,
    dependencies_sha256="c" * 64,
    runtime="process-example-unit-test",
)


class ProcessShopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.artifacts = {
            example.id: compile_shop(define_shop(example), IDENTITY) for example in EXAMPLES
        }

    def test_all_technical_and_business_acceptance_cases(self):
        for example in EXAMPLES:
            artifact = self.artifacts[example.id]
            with self.subTest(process=example.id):
                for result in run_cases(artifact, scenarios(example)):
                    self.assertTrue(result.passed, f"{example.id}: {result.model_dump_json()}")
                self.assertEqual(read_artifact(artifact.to_json()), artifact)
                self.assertEqual({proof.source for proof in artifact.shop.evidence}, {"simulated"})

    def test_technical_capability_does_not_imply_business_acceptance(self):
        for example in EXAMPLES:
            with self.subTest(process=example.id):
                technical = evaluate(
                    self.artifacts[example.id],
                    request(example),
                    at=AT,
                    observations=example.observations(),
                )
                production = evaluate(
                    self.artifacts[example.id],
                    request(
                        example,
                        engagement="production",
                        changes={"lot-quantity": quantity("1", "1")},
                    ),
                    at=AT,
                    observations=example.observations(),
                )
                prototype = evaluate(
                    self.artifacts[example.id],
                    request(example, engagement="prototype"),
                    at=AT,
                    observations=example.observations(),
                )
                self.assertEqual(
                    (technical.outcome, production.outcome, prototype.outcome),
                    ("supported", "unsupported", "supported"),
                )

    def test_contextual_business_variables_preserve_scope_and_uncertainty(self):
        for example in EXAMPLES:
            artifact = self.artifacts[example.id]
            with self.subTest(process=example.id):
                no_context = query_variables(
                    artifact,
                    VariableQuery(ids=("minimum-production-lot",)),
                    at=AT,
                ).variables[0]
                self.assertEqual(no_context.applicability, "unknown")
                self.assertIsNone(no_context.effective_value)
                prototype = query_variables(
                    artifact,
                    VariableQuery(
                        ids=("minimum-production-lot",),
                        context=(Fact(name="engagement", value="prototype"),),
                    ),
                    at=AT,
                ).variables[0]
                self.assertEqual(prototype.applicability, "not-applicable")
                annual = query_variables(
                    artifact,
                    VariableQuery(
                        ids=("finished-goods-storage",),
                        context=(Fact(name="engagement", value="annual-contract"),),
                    ),
                    at=AT,
                ).variables[0]
                self.assertEqual(annual.applicability, "applicable")
                self.assertEqual(annual.effective_value, TERMS[example.policy].releases)
                result = query_variables(
                    artifact,
                    VariableQuery(
                        ids=(
                            "typical-turnaround",
                            "minimum-charge",
                            "annual-capacity",
                            "customer-qualification",
                        )
                    ),
                    at=AT,
                )
                for finding in result.variables:
                    if finding.definition["id"] == "typical-turnaround":
                        self.assertEqual(finding.definition["certainty"], "typical")
                    else:
                        self.assertEqual(finding.definition["certainty"], "unknown")
                        self.assertIsNone(finding.effective_value)

    def test_real_process_differences_are_executable(self):
        # Independent expectations: neither relabeling a machine nor widening a
        # range can establish the missing process, tool, input, or evidence.
        probes = (
            ("milling-indexed", "motion-mode", "simultaneous", "unsupported"),
            ("swiss-turning", "bar-condition", "unreviewed-hot-rolled", "unsupported"),
            ("wire-edm", "electrical-behavior", "insulating", "unsupported"),
            ("centerless-grinding", "feed-mode", "plunge", "unsupported"),
            ("tapping", "hole-type", "blind", "unsupported"),
            ("reaming", "reaming-allowance", quantity("0.5", "mm"), "unsupported"),
            ("press-brake", "tonnage-springback-and-sequence-reviewed", False, "unknown"),
            ("injection-molding", "fill-clamp-and-cooling-reviewed", False, "unknown"),
        )
        for id, fact, value, expected in probes:
            example = BY_ID[id]
            with self.subTest(process=id):
                result = evaluate(
                    self.artifacts[id],
                    request(example, changes={fact: value}),
                    at=AT,
                    observations=example.observations(),
                )
                self.assertEqual(result.outcome, expected)

    def test_published_contract_contains_terms_but_no_private_equipment_details(self):
        for example in EXAMPLES:
            with self.subTest(process=example.id):
                catalog = public_catalog(self.artifacts[example.id])
                self.assertNotIn("private-equipment.example.invalid", str(catalog))
                self.assertNotIn("controller-address", str(catalog))
                self.assertEqual(
                    {item["id"] for item in catalog["capabilities"]},
                    {example.id, "terms-prototype", "terms-production"},
                )
                production = next(
                    c for c in catalog["capabilities"] if c["id"] == "terms-production"
                )
                quantity_rule = next(
                    r for r in production["requirements"] if r["fact"] == "lot-quantity"
                )
                self.assertEqual(quantity_rule["multiple_of"], {"value": "1", "unit": "1"})
                workflow = next(w for w in catalog["workflows"] if w["id"] == "offer-production")
                self.assertEqual(set(workflow["capabilities"]), {example.id, "terms-production"})

    def test_mixed_shop_shares_equipment_but_checks_explicit_routes(self):
        artifact = compile_shop(mixed.define_shop(), IDENTITY)
        for result in run_cases(artifact, mixed.scenarios()):
            self.assertTrue(result.passed, result.model_dump_json())
        by_id = {cap.id: cap for cap in artifact.shop.capabilities}
        for id in ("milling-3axis", "drilling", "reaming"):
            self.assertIn("shared-vmc", by_id[id].resources)
        self.assertNotIn("shared-vmc", by_id["swiss-turning"].resources)
        self.assertEqual(len([r for r in artifact.shop.resources if r.id == "shared-vmc"]), 1)

    def test_source_projects_are_self_contained_and_include_acceptance_cases(self):
        for id in (*BY_ID, "mixed"):
            with self.subTest(process=id):
                source = project(id)
                self.assertGreater(len(source.files), 1)
                self.assertGreater(len(source.tests), 1)
                self.assertTrue(all("from examples" not in file.content for file in source.files))
                restored = source.model_validate_json(source.to_json())
                self.assertEqual(source.to_json(), restored.to_json())
                self.assertEqual(source.revision, restored.revision)


class QuantityIncrementTests(unittest.TestCase):
    def test_optional_increment_preserves_existing_artifact_encoding(self):
        example = BY_ID["milling-3axis"]
        artifact = compile_shop(define_shop(example), IDENTITY)
        legacy = artifact.model_dump(mode="json")
        for capability in legacy["shop"]["capabilities"]:
            for rule in capability["requirements"]:
                rule.pop("multiple_of", None)
        for proof in legacy["shop"]["evidence"]:
            for rule in proof["requirements"]:
                rule.pop("multiple_of", None)
        legacy["model_sha256"] = digest(canonical_json(legacy["shop"]))
        legacy["build_sha256"] = digest(
            canonical_json(
                {
                    "model_sha256": legacy["model_sha256"],
                    "identity": legacy["identity"],
                }
            )
        )
        restored = read_artifact(canonical_json(legacy))
        self.assertEqual(restored.model_sha256, legacy["model_sha256"])
        self.assertEqual(restored.to_json(), canonical_json(legacy))

    def test_integer_counts_and_exact_cross_unit_increments(self):
        count = Range(fact="count", minimum=quantity("1", "1"), multiple_of=quantity("1", "1"))
        for value in ("1", "100", "100.000"):
            self.assertIsNone(check_rule(count, {"count": quantity(value, "1")}, ()))
        for value in ("1.5", "100.000000000000000000000000001"):
            failure = check_rule(count, {"count": quantity(value, "1")}, ())
            self.assertEqual((failure.outcome, failure.code), ("unsupported", "invalid-increment"))
        length = Range(
            fact="cut-length",
            minimum=quantity("0", "mm"),
            maximum=quantity("100", "mm"),
            multiple_of=quantity("0.1", "[in_i]"),
        )
        self.assertIsNone(check_rule(length, {"cut-length": quantity("25.4", "mm")}, ()))
        self.assertEqual(
            check_rule(length, {"cut-length": quantity("25.40000000000000001", "mm")}, ()).code,
            "invalid-increment",
        )

    def test_invalid_increments_are_rejected_and_missing_values_remain_unknown(self):
        for step in (quantity("0", "1"), quantity("-1", "1"), quantity("1", "mm")):
            with self.subTest(step=step), self.assertRaises(ValidationError):
                Range(fact="count", minimum=quantity("1", "1"), multiple_of=step)
        with self.assertRaises(ValidationError):
            Range(fact="temperature", minimum=quantity("0", "Cel"), multiple_of=quantity("1", "K"))
        rule = Range(fact="count", minimum=quantity("1", "1"), multiple_of=quantity("1", "1"))
        self.assertEqual(check_rule(rule, {}, ()).outcome, "unknown")
        self.assertEqual(
            check_rule(rule, {"count": quantity("2", "mm")}, ()).code, "incompatible-units"
        )

    def test_increment_is_preserved_in_evidence_scope(self):
        example = BY_ID["milling-3axis"]
        shop = define_shop(example)
        caps = tuple(
            cap.model_copy(
                update={
                    "requirements": tuple(
                        rule.model_copy(update={"multiple_of": None})
                        if isinstance(rule, Range)
                        else rule
                        for rule in cap.requirements
                    )
                }
            )
            if cap.id == "terms-production"
            else cap
            for cap in shop.capabilities
        )
        artifact = compile_shop(shop.model_copy(update={"capabilities": caps}), IDENTITY)
        result = evaluate(
            artifact,
            request(
                example, engagement="production", changes={"lot-quantity": quantity("250.5", "1")}
            ),
            at=AT,
            observations=example.observations(),
        )
        self.assertEqual(result.outcome, "unknown")
        self.assertIn("evidence-unavailable", {reason.code for reason in result.reasons})
