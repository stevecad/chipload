"""Semantic identity, public disclosure and business-rule preservation for listings."""

import unittest

from pydantic import ValidationError

from chipload import (
    BuildIdentity,
    GeoPoint,
    SemanticRef,
    Shop,
    ShopValidationError,
    compile_shop,
    evaluate,
    read_artifact,
)
from chipload.artifact import canonical_json
from chipload.discovery import public_catalog
from chipload.listings import ListingInput, ListingQuery, translate
from examples.federation_shops import AT, PROFILES, cases, define_shop, query, term

IDENTITY = BuildIdentity(source_sha256="a" * 64, sdk_sha256="b" * 64,
                         dependencies_sha256="c" * 64, runtime="listing-test")


class ListingTests(unittest.TestCase):
    def test_existing_shop_bytes_remain_unchanged_without_optional_fields(self):
        shop = Shop(id="legacy", name="Legacy")
        expected = {"id": "legacy", "name": "Legacy", "description": "", "visibility": "private",
                    "schema_version": "0.2-draft", **{name: [] for name in (
                        "sites", "resources", "catalogs", "processes", "capabilities",
                        "workflows", "evidence", "variables")}}
        self.assertEqual(canonical_json(shop), canonical_json(expected))
        artifact = compile_shop(shop, IDENTITY)
        self.assertEqual(read_artifact(artifact.to_json()), artifact)

    def test_distinct_shop_families_use_the_same_mapping_and_business_evaluator(self):
        for name, config in PROFILES.items():
            with self.subTest(profile=name):
                artifact = compile_shop(define_shop(name), IDENTITY)
                self.assertEqual(read_artifact(artifact.to_json()), artifact)
                for case in cases(name):
                    self.assertEqual(evaluate(artifact, case.request, at=AT).outcome, case.expected)
                request = query(name, revision=public_catalog(artifact)["public_sha256"],
                                count=config["minimum_quantity"])
                translated = translate(artifact.shop.listings[0], request)
                self.assertEqual(evaluate(artifact, translated, at=AT).outcome, "supported")

    def test_nearest_supplier_can_fail_quantity_and_material_supply_is_distinct(self):
        outcomes = []
        for name in ("supplier-near", "supplier-far"):
            artifact = compile_shop(define_shop(name), IDENTITY)
            translated = translate(artifact.shop.listings[0], query(name, revision="a" * 64))
            outcomes.append(evaluate(artifact, translated, at=AT).outcome)
        self.assertEqual(outcomes, ["unsupported", "supported"])
        self.assertIsNone(translate(define_shop("mill").listings[0], query(revision="a" * 64)))

    def test_namespace_version_and_value_are_exact_and_unknown_inputs_are_not_dropped(self):
        listing = define_shop().listings[0]
        request = query(revision="a" * 64)
        first = request.inputs[0]
        for value in ("aluminum-6061", term("aluminum-7075"),
                      term("aluminum-6061").model_copy(update={"version": "2"}),
                      term("aluminum-6061").model_copy(update={"namespace": "other.example"})):
            changed = request.model_copy(update={"inputs": (first.model_copy(update={"value": value}), *request.inputs[1:])})
            self.assertIsNone(translate(listing, changed))
        self.assertIsNone(translate(listing, request.model_copy(update={"inputs": (
            *request.inputs, ListingInput(term=term("unmodeled-requirement"), value=True),
        )})))
        self.assertIsNone(translate(listing, request.model_copy(update={"profile": term("milling-service")})))

    def test_duplicate_semantic_inputs_and_bindings_are_rejected(self):
        request = query(revision="a" * 64)
        with self.assertRaises(ValidationError):
            ListingQuery.model_validate(request.model_copy(update={"inputs": request.inputs * 2}))
        shop = define_shop()
        listing = shop.listings[0]
        for fields in ((listing.fields[0], listing.fields[0]),
                       (listing.fields[0], listing.fields[0].model_copy(update={"term": term("alias")}))):
            with self.assertRaises(ValidationError):
                compile_shop(shop.model_copy(update={"listings": (listing.model_copy(update={"fields": fields}),)}), IDENTITY)

    def test_invalid_references_and_private_input_mappings_cannot_publish(self):
        shop = define_shop()
        listing = shop.listings[0]
        variants = [
            shop.model_copy(update={"listings": (listing.model_copy(update={"target": "missing"}),)}),
            shop.model_copy(update={"listings": (listing.model_copy(update={"site": "missing"}),)}),
            shop.model_copy(update={"sites": (shop.sites[0].model_copy(update={"visibility": "private"}),)}),
            shop.model_copy(update={"workflows": (shop.workflows[0].model_copy(update={"visibility": "private"}),)}),
            shop.model_copy(update={"visibility": "private"}),
            shop.model_copy(update={"listings": (listing.model_copy(update={"fields": (
                listing.fields[0].model_copy(update={"fact": "typo"}),
            )}),)}),
        ]
        catalog = shop.catalogs[0]
        for hidden in (catalog.model_copy(update={"visibility": "private"}),
                       catalog.model_copy(update={"items": (catalog.items[0].model_copy(update={"visibility": "private"}),)})):
            variants.append(shop.model_copy(update={"catalogs": (hidden, *shop.catalogs[1:])}))
        for changed in variants:
            with self.subTest(shop=changed.id), self.assertRaises(ShopValidationError):
                compile_shop(changed, IDENTITY)

    def test_mapping_and_location_changes_affect_public_revision_but_private_listings_do_not(self):
        shop = define_shop()
        original = public_catalog(compile_shop(shop, IDENTITY))
        private = shop.listings[0].model_copy(update={"id": "private", "indexed": False, "visibility": "private"})
        self.assertEqual(original, public_catalog(compile_shop(shop.model_copy(update={"listings": (*shop.listings, private)}), IDENTITY)))
        site = shop.sites[0].model_copy(update={"public_location": GeoPoint(latitude="42", longitude="-88", accuracy_m=1000)})
        changed = public_catalog(compile_shop(shop.model_copy(update={"sites": (site,)}), IDENTITY))
        self.assertNotEqual(original["public_sha256"], changed["public_sha256"])
        field = shop.listings[0].fields[-1].model_copy(update={"term": term("new-documentation-meaning")})
        listing = shop.listings[0].model_copy(update={"fields": (*shop.listings[0].fields[:-1], field)})
        changed = public_catalog(compile_shop(shop.model_copy(update={"listings": (listing,)}), IDENTITY))
        self.assertNotEqual(original["public_sha256"], changed["public_sha256"])

    def test_location_ranges_units_and_accuracy_are_explicit_and_bounded(self):
        self.assertEqual(GeoPoint(latitude="-0.000", longitude="180.0", accuracy_m=0).latitude, "0")
        for changes in ({"latitude": "90.0000001"}, {"longitude": "-181"}, {"latitude": 1},
                        {"latitude": "NaN"}, {"longitude": "1e2"}, {"accuracy_m": -1}, {"accuracy_m": True}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                GeoPoint.model_validate({"latitude": "0", "longitude": "0", "accuracy_m": 100, **changes})
        with self.assertRaises(ValidationError):
            SemanticRef(namespace="https://untrusted.example", version="1", term="grade")

    def test_indexing_is_separate_from_public_visibility(self):
        shop = define_shop()
        listing = shop.listings[0].model_copy(update={"indexed": False})
        projected = public_catalog(compile_shop(shop.model_copy(update={"listings": (listing,)}), IDENTITY))
        self.assertFalse(projected["listings"][0]["indexed"])
        with self.assertRaises(ValidationError):
            compile_shop(shop.model_copy(update={"listings": (
                listing.model_copy(update={"visibility": "private", "indexed": True}),
            )}), IDENTITY)
