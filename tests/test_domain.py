import json
import unittest
from datetime import datetime, timedelta
from decimal import localcontext

from pydantic import ValidationError

from chipload import (
    Choice,
    Fact,
    Interval,
    Observation,
    Range,
    Shop,
    ShopValidationError,
    Step,
    Workflow,
    compile_shop,
    evaluate,
    quantity,
    read_artifact,
)
from chipload.discovery import public_catalog
from chipload.evaluate import same
from examples.reference_shop.scenarios import AT, SCENARIOS, observations, request
from examples.reference_shop.shop import build
from examples.swiss_profile import define_shop as swiss_shop


class DomainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.artifact = build()

    def compile(self, shop):
        return compile_shop(shop, self.artifact.identity)

    def replaced(self, collection, index, **changes):
        values = list(getattr(self.artifact.shop, collection))
        values[index] = values[index].model_copy(update=changes)
        return self.artifact.shop.model_copy(update={collection: tuple(values)})

    def result(self, query=None, **kwargs):
        return evaluate(self.artifact, query or request(), at=AT, **kwargs)

    def assert_code(self, result, code):
        self.assertIn(code, {reason.code for reason in result.reasons})

    def test_manufacturing_acceptance_scenarios(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario.name):
                result = self.result(scenario.question, observations=scenario.observation_snapshot)
                self.assertEqual(result.outcome, scenario.expected, scenario.explanation)
                self.assert_code(result, scenario.reason)

    def test_quantity_conversions_are_exact_at_boundaries(self):
        self.assertTrue(same(quantity("1", "[in_i]"), quantity("25.4", "mm")))
        self.assertFalse(same(quantity("1.25", "[in_i]"), quantity("32", "mm")))
        self.assertEqual(
            self.result(request(changes={"length": quantity("30", "cm")})).outcome, "supported"
        )
        result = self.result(request(changes={"length": quantity("30.0000000000000001", "cm")}))
        self.assertEqual(result.outcome, "unsupported")
        with localcontext() as context:
            context.prec = 2
            self.assertTrue(same(quantity("1", "[in_i]"), quantity("25.4", "mm")))
            self.assertGreater(
                quantity("300.0000000000000000000000001", "mm").base_value(),
                quantity("300", "mm").base_value(),
            )

    def test_quantity_does_not_coerce_ambiguous_numbers(self):
        for value in (1, 1.1, True, "1e3", "NaN", "Infinity", "01", " 1", "+1"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                quantity(value, "mm")
        with self.assertRaises(ValidationError):
            quantity("1", "inches")
        self.assertEqual(quantity("1.000", "mm").value, "1")
        self.assertEqual(quantity("-0.00", "mm").value, "0")

    def test_invalid_bounds_fail_at_authoring_time(self):
        for minimum, maximum in (
            (quantity("2", "mm"), quantity("1", "mm")),
            (quantity("1", "mm"), quantity("2", "kg")),
        ):
            with self.assertRaises(ValidationError):
                Range(fact="length", minimum=minimum, maximum=maximum)
            with self.assertRaises(ValidationError):
                Interval(minimum=minimum, maximum=maximum)

    def test_unknown_fields_and_missing_remedies_are_errors(self):
        with self.assertRaises(ValidationError):
            quantity("1", "mm").model_validate({"value": "1", "unit": "mm", "unt": "mm"})
        with self.assertRaises(ValidationError):
            Choice(fact="fixture", allowed=("vise",), on_mismatch="conditional")

    def test_incompatible_units_never_prove_support(self):
        result = self.result(request(changes={"length": quantity("100", "kg")}))
        self.assertEqual(result.outcome, "unknown")
        self.assert_code(result, "incompatible-units")

    def test_fixture_remedy_is_explicit_and_does_not_mutate_request(self):
        query = request("mill-wide", changes={"length": quantity("400", "mm")})
        result = self.result(query)
        self.assertEqual(result.outcome, "conditional")
        self.assertIn(Fact(name="fixture", value="wide-fixture"), result.assumptions)
        self.assertIn(Fact(name="fixture", value="flat-vise"), query.facts)

    def test_missing_knowledge_dominates_a_conditional_remedy(self):
        result = self.result(
            request(
                "mill-wide", changes={"length": quantity("400", "mm"), "geometry-reviewed": None}
            )
        )
        self.assertEqual(result.outcome, "unknown")
        self.assert_code(result, "missing-fact")
        self.assert_code(result, "value-not-supported")

    def test_declared_evidence_cannot_prove_qualification(self):
        result = self.result(request().model_copy(update={"minimum_evidence": "qualified"}))
        self.assertEqual(result.outcome, "unknown")

    def test_widening_a_capability_does_not_widen_its_evidence(self):
        rules = list(self.artifact.shop.capabilities[0].requirements)
        index = next(i for i, rule in enumerate(rules) if rule.fact == "length")
        rules[index] = Range(
            fact="length", minimum=quantity("0.001", "mm"), maximum=quantity("350", "mm")
        )
        artifact = self.compile(self.replaced("capabilities", 0, requirements=tuple(rules)))
        result = evaluate(artifact, request(changes={"length": quantity("340", "mm")}), at=AT)
        self.assertEqual(result.outcome, "unknown")
        self.assert_code(result, "evidence-unavailable")

    def test_evidence_needs_all_facts_and_resources(self):
        proof = self.artifact.shop.evidence[0]
        for changes in ({"requirements": proof.requirements[1:]}, {"resources": ("mill-a",)}):
            with self.subTest(changes=changes):
                artifact = self.compile(self.replaced("evidence", 0, **changes))
                self.assertEqual(evaluate(artifact, request(), at=AT).outcome, "unknown")

    def test_future_or_expired_evidence_is_not_current(self):
        for changes in (
            {"recorded_at": AT + timedelta(seconds=1)},
            {"valid_until": AT},
        ):
            with self.subTest(changes=changes):
                artifact = self.compile(self.replaced("evidence", 0, **changes))
                self.assertEqual(evaluate(artifact, request(), at=AT).outcome, "unknown")

    def test_observation_expiry_is_exclusive_and_future_is_not_current(self):
        observed = observations()[-1]
        for time, expected in (
            (AT - timedelta(seconds=3600), "observation-stale"),
            (AT + timedelta(seconds=1), "observation-future"),
        ):
            result = self.result(
                request("inspect-bare"), observations=(observed.model_copy(update={"time": time}),)
            )
            self.assertEqual(result.outcome, "unknown")
            self.assert_code(result, expected)

    def test_observation_identity_and_unknown_value_are_checked(self):
        observed = observations()[-1]
        for changes, code in (
            ({"resource": "mill-a"}, "observation-mismatched"),
            ({"property": "available"}, "observation-mismatched"),
            ({"value": None}, "observation-unknown"),
        ):
            with self.subTest(changes=changes):
                result = self.result(
                    request("inspect-bare"), observations=(observed.model_copy(update=changes),)
                )
                self.assertEqual(result.outcome, "unknown")
                self.assert_code(result, code)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.result(observations=(observed, observed))

    def test_simulation_is_explicit_in_results(self):
        self.assertTrue(self.result().simulated)
        self.assertEqual(
            self.result(request().model_copy(update={"allow_simulated": False})).outcome, "unknown"
        )

    def test_timestamp_inputs_require_explicit_zones(self):
        fields = observations()[0].model_dump()
        for value in (0, True, "2026-10-20", "2026-10-20T12:00:00", datetime(2026, 10, 20)):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                Observation(**{**fields, "time": value})
        observed = Observation(**{**fields, "time": "2026-10-20T07:00:00-05:00"})
        self.assertEqual(observed.time, AT)
        self.assertEqual(observed.time.utcoffset(), timedelta(0))
        with self.assertRaises(ValueError):
            evaluate(self.artifact, request(), at=datetime(2026, 10, 20))

    def test_models_are_immutable(self):
        with self.assertRaises(ValidationError):
            self.artifact.shop.name = "Other"
        with self.assertRaises(ValidationError):
            self.artifact.shop.capabilities[0].requirements[1].maximum.value = "999"

    def test_duplicate_ids_and_broken_references_have_repair_locations(self):
        cases = (
            (
                self.artifact.shop.model_copy(
                    update={"resources": self.artifact.shop.resources * 2}
                ),
                "duplicate-id",
                "resources",
            ),
            (self.replaced("resources", 0, site="missing-site"), "missing-reference", "site"),
            (
                self.replaced("capabilities", 0, evidence=("deburr-declaration",)),
                "evidence-scope",
                "evidence",
            ),
        )
        for shop, code, path_part in cases:
            with self.subTest(code=code), self.assertRaises(ShopValidationError) as caught:
                self.compile(shop)
            self.assertTrue(
                any(d.code == code and path_part in d.path for d in caught.exception.diagnostics)
            )

    def test_resource_and_workflow_cycles_are_rejected(self):
        resource_cycle = self.replaced("resources", 0, parent="mill-a")
        workflow = self.artifact.shop.workflows[0]
        steps = (workflow.steps[0].model_copy(update={"after": ("finish",)}), *workflow.steps[1:])
        workflow_cycle = self.replaced("workflows", 0, steps=steps)
        for shop in (resource_cycle, workflow_cycle):
            with self.assertRaises(ShopValidationError) as caught:
                self.compile(shop)
            self.assertIn("dependency-cycle", {d.code for d in caught.exception.diagnostics})

    def test_compilation_revalidates_unchecked_copies(self):
        with self.assertRaises(ValidationError):
            self.compile(self.replaced("resources", 0, kind="invalid kind with spaces"))

    def test_conflicting_workflow_branches_cannot_be_merged(self):
        workflow = Workflow(
            id="conflicting",
            name="Two incompatible workpiece states",
            steps=(
                Step(id="bare", capability="mill-small"),
                Step(id="anodized", capability="anodize"),
                Step(id="inspect", capability="inspect-bare", after=("bare", "anodized")),
            ),
        )
        artifact = self.compile(self.artifact.shop.model_copy(update={"workflows": (workflow,)}))
        result = evaluate(
            artifact,
            request("conflicting", target_kind="workflow"),
            at=AT,
            observations=observations(),
        )
        self.assertEqual(result.outcome, "unsupported")
        self.assert_code(result, "conflicting-input-state")

    def test_artifact_round_trip_and_reproducible_build(self):
        artifact = read_artifact(self.artifact.to_json())
        self.assertEqual(artifact, self.artifact)
        self.assertEqual(build().build_sha256, self.artifact.build_sha256)
        self.assertEqual(Shop.model_validate_json(artifact.shop.model_dump_json()), artifact.shop)
        self.assertEqual(Shop.model_json_schema()["additionalProperties"], False)

    def test_changed_source_identity_and_model_have_distinct_hashes(self):
        identity = self.artifact.identity.model_copy(update={"source_sha256": "a" * 64})
        artifact = compile_shop(self.artifact.shop, identity)
        self.assertEqual(artifact.model_sha256, self.artifact.model_sha256)
        self.assertNotEqual(artifact.build_sha256, self.artifact.build_sha256)
        modified = self.compile(self.artifact.shop.model_copy(update={"name": "Another shop"}))
        self.assertNotEqual(modified.model_sha256, self.artifact.model_sha256)

    def test_artifact_tampering_duplicate_keys_and_size_are_rejected(self):
        data = json.loads(self.artifact.to_json())
        data["shop"]["name"] = "Tampered"
        with self.assertRaisesRegex(ValueError, "digest"):
            read_artifact(json.dumps(data).encode())
        with self.assertRaisesRegex(ValueError, "duplicate"):
            read_artifact(b'{"shop": {}, "shop": {}}')
        with self.assertRaisesRegex(ValueError, "size"):
            read_artifact(self.artifact.to_json(), max_bytes=10)

    def test_public_catalog_excludes_internal_information(self):
        public = json.dumps(public_catalog(self.artifact))
        for hidden in (
            "PRIVATE-CUSTOMER-PROJECT",
            "research-cell",
            "PRIVATE-SUPPLIER-42",
            "machine.internal.invalid",
            "shops/riverside",
            "private_details",
            "dependencies_sha256",
            "source_sha256",
            "finishing-partner",
            "invalid-finish-before-inspection",
        ):
            with self.subTest(hidden=hidden):
                self.assertNotIn(hidden, public)
        self.assertIn("mill-small", public)

    def test_private_changes_do_not_change_public_identity(self):
        artifact = self.compile(
            self.replaced(
                "resources",
                0,
                private_details=(Fact(name="address", value="new.internal.invalid"),),
            )
        )
        self.assertNotEqual(artifact.model_sha256, self.artifact.model_sha256)
        self.assertEqual(public_catalog(artifact), public_catalog(self.artifact))
        with self.assertRaises(KeyError):
            public_catalog(
                self.compile(self.artifact.shop.model_copy(update={"visibility": "private"}))
            )

    def test_swiss_profile_preserves_business_variables_and_uncertainty(self):
        shop = swiss_shop()
        artifact = self.compile(shop)
        self.assertEqual(read_artifact(artifact.to_json()).shop, shop)
        variables = {variable.id: variable for variable in shop.variables}
        self.assertEqual(variables["minimum-order-quantity"].certainty, "unknown")
        self.assertIsNone(variables["minimum-order-quantity"].value)
        self.assertEqual(variables["usual-maximum-length"].certainty, "typical")
        self.assertEqual(variables["reported-part-diameter"].certainty, "approximate")
        self.assertFalse(
            same(
                variables["reported-part-diameter"].value.maximum,
                variables["reported-max-diameter-inch"].value,
            )
        )
        self.assertTrue(variables["finished-goods-inventory"].value)
        self.assertTrue(variables["monthly-releases"].applies_when)
        self.assertEqual(shop.resources, ())
        self.assertEqual(shop.capabilities, ())
        self.assertEqual(shop.sites, ())
        # Descriptive variables do not invent qualified capabilities.
        self.assertEqual(public_catalog(artifact)["capabilities"], [])

    def test_lack_of_geometry_review_is_uncertainty(self):
        result = self.result(request(changes={"geometry-reviewed": False}))
        self.assertEqual(result.outcome, "unknown")

    def test_retired_resources_do_not_remain_available_as_conditional_offers(self):
        artifact = self.compile(self.replaced("resources", 0, status="retired"))
        result = evaluate(artifact, request(), at=AT)
        self.assertEqual(result.outcome, "unsupported")
        self.assert_code(result, "resource-retired")

    def test_unknown_shop_variables_cannot_contain_guessed_values(self):
        variables = swiss_shop().variables
        unknown = next(v for v in variables if v.certainty == "unknown")
        with self.assertRaises(ValidationError):
            self.compile(
                swiss_shop().model_copy(
                    update={
                        "variables": (unknown.model_copy(update={"value": quantity("1", "1")}),)
                    }
                )
            )


if __name__ == "__main__":
    unittest.main()
