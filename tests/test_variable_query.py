import unittest
from datetime import timedelta

from chipload import (
    BuildIdentity,
    Catalog,
    CatalogItem,
    Choice,
    Fact,
    Shop,
    ShopVariable,
    VariableQuery,
    compile_shop,
    query_variables,
)
from examples.swiss_profile import RECORDED, define_shop

IDENTITY = BuildIdentity(
    source_sha256="a" * 64, sdk_sha256="b" * 64, dependencies_sha256="c" * 64, runtime="unit-test"
)


class VariableQueryTests(unittest.TestCase):
    def setUp(self):
        self.artifact = compile_shop(define_shop(), IDENTITY)

    def query(self, *ids, context=()):
        return query_variables(self.artifact, VariableQuery(ids=ids, context=context), at=RECORDED)

    def test_a_conditional_offer_is_not_unconditional(self):
        result = self.query("monthly-releases")
        finding = result.variables[0]
        self.assertEqual(finding.applicability, "unknown")
        self.assertIsNone(finding.effective_value)
        self.assertIn("missing-fact", {r.code for r in finding.reasons})

    def test_context_selects_applicability_without_negating_the_declared_offer(self):
        for engagement, status, effective in (
            ("annual-contract", "applicable", True),
            ("prototype", "not-applicable", None),
        ):
            result = self.query(
                "monthly-releases", context=(Fact(name="engagement", value=engagement),)
            )
            finding = result.variables[0]
            self.assertEqual(finding.applicability, status)
            self.assertEqual(finding.effective_value, effective)
            self.assertTrue(finding.definition["value"])

    def test_approximate_typical_and_unknown_values_keep_their_meaning(self):
        result = self.query(
            "reported-part-diameter", "usual-maximum-length", "minimum-order-quantity"
        )
        self.assertEqual(
            [f.definition["certainty"] for f in result.variables],
            ["approximate", "typical", "unknown"],
        )
        self.assertIsNone(result.variables[-1].effective_value)
        self.assertIn("unknown-value", {r.code for r in result.variables[-1].reasons})

    def test_future_and_expired_statements_are_not_current(self):
        original = define_shop().variables[0]
        for recorded, expiry in (
            (RECORDED + timedelta(seconds=1), None),
            (RECORDED - timedelta(days=1), RECORDED),
        ):
            variable = original.model_copy(update={"recorded_at": recorded, "valid_until": expiry})
            artifact = compile_shop(
                define_shop().model_copy(update={"variables": (variable,)}), IDENTITY
            )
            result = query_variables(artifact, VariableQuery(), at=RECORDED)
            self.assertEqual(result.variables[0].applicability, "unknown")
            self.assertIsNone(result.variables[0].effective_value)

    def test_private_and_missing_ids_have_the_same_public_behavior(self):
        original = define_shop().variables[0]
        variable = original.model_copy(update={"visibility": "private"})
        artifact = compile_shop(
            define_shop().model_copy(update={"variables": (variable,)}), IDENTITY
        )
        for id in (variable.id, "not-present"):
            with self.assertRaisesRegex(KeyError, "unknown variable"):
                query_variables(artifact, VariableQuery(ids=(id,)), at=RECORDED)
        private = query_variables(artifact, VariableQuery(), at=RECORDED, public_only=False)
        self.assertEqual(len(private.variables), 1)

    def test_private_conditions_cannot_be_queried_as_a_membership_oracle(self):
        catalog = Catalog(
            id="customer-tiers",
            name="Private customer list",
            items=(CatalogItem(id="secret-customer", name="Private customer"),),
        )
        variable = ShopVariable(
            id="special-arrangement",
            name="Special arrangements",
            category="business-policy",
            value=True,
            certainty="declared",
            source="person",
            recorded_at=RECORDED,
            visibility="public",
            applies_when=(
                Choice(fact="customer", catalog=catalog.id, allowed=("secret-customer",)),
            ),
        )
        artifact = compile_shop(
            Shop(
                id="example",
                name="Example",
                visibility="public",
                catalogs=(catalog,),
                variables=(variable,),
            ),
            IDENTITY,
        )
        for customer in ("secret-customer", "another-customer"):
            result = query_variables(
                artifact,
                VariableQuery(context=(Fact(name="customer", value=customer),)),
                at=RECORDED,
            )
            self.assertEqual(result.variables[0].applicability, "unknown")
            self.assertNotIn("secret-customer", result.model_dump_json())
            self.assertNotIn("customer-tiers", result.model_dump_json())

    def test_unknown_context_is_reported_without_becoming_a_requirement(self):
        result = self.query("prototype-work", context=(Fact(name="unrelated", value=True),))
        self.assertEqual(result.unused_context, ("unrelated",))
        self.assertEqual(result.variables[0].applicability, "applicable")


if __name__ == "__main__":
    unittest.main()
