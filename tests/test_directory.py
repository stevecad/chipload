"""Exact cross-shop supplier search, leases, CAS and durable withdrawal fences."""

import tempfile
import unittest
import uuid
from datetime import timedelta
from pathlib import Path

from chipload import BuildIdentity, GeoPoint, Site, compile_shop
from chipload.auth import ServerSettings
from chipload.directory import DirectoryCapacity, DirectoryStore, distance
from chipload.directory_model import (
    Advertisement,
    CloudSettings,
    OwnerBinding,
    SupplierSearch,
    Withdrawal,
)
from chipload.discovery import public_catalog
from chipload.lifecycle import Conflict
from chipload.peer_model import PeerResult
from examples.federation_shops import AT, define_shop, query, term


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.now = AT
        self.first, self.second = "a" * 32, "b" * 32
        self.settings = CloudSettings(directory_id="c" * 32, identity=ServerSettings(
            database=Path(temporary.name) / "private/directory.db", resource_url="https://cloud.example/mcp",
            issuer="https://identity.example/realms/cloud", jwks_url="https://identity.example/keys",
        ), bindings=(OwnerBinding(subject="owner-a", instance_id=self.first), OwnerBinding(subject="owner-b", instance_id=self.second)))
        self.store = DirectoryStore(self.settings, create=True, clock=lambda: self.now)

    def advertisement(self, profile="supplier-far", *, version=0, generation=1, shop=None):
        artifact = compile_shop(shop or define_shop(profile), BuildIdentity(source_sha256="a" * 64,
            sdk_sha256="b" * 64, dependencies_sha256="c" * 64, runtime="directory-fixture"))
        catalog = public_catalog(artifact)
        return Advertisement(request_id=uuid.uuid4().hex, expected_version=version, observed_at=self.now,
            catalog=PeerResult(action="catalog", generation=generation, revision=catalog["public_sha256"], value=catalog))

    def search(self, **updates):
        inputs = query(revision="a" * 64).inputs
        return SupplierSearch(profile=term("material-supply"), inputs=inputs,
            origin=GeoPoint(latitude="41.88", longitude="-87.63", accuracy_m=50), radius_m=100_000, **updates)

    def test_nearest_site_and_materials_match_without_inventing_business_acceptance(self):
        self.store.advertise(self.first, "supplier-near", "owner-a", self.advertisement("supplier-near"))
        self.store.advertise(self.second, "supplier-far", "owner-b", self.advertisement())
        page = self.store.search(self.search(), connected=frozenset({self.first}))
        self.assertEqual([c.shop_id for c in page.candidates], ["supplier-near", "supplier-far"])
        self.assertLess(page.candidates[0].distance_m, page.candidates[1].distance_m)
        self.assertEqual(page.candidates[0].distance_uncertainty_m, 150)
        self.assertTrue(page.candidates[0].relay_available)
        self.assertFalse(page.candidates[1].relay_available)
        # Quantities are evaluated at the supplier, not guessed from index membership.
        self.assertNotIn("outcome", page.candidates[0].model_dump())
        self.assertNotIn("fields", page.candidates[0].listing.model_dump())

    def test_namespace_version_supply_and_processing_are_distinct(self):
        self.store.advertise(self.first, "supplier-far", "owner-a", self.advertisement())
        base = self.search()
        for profile in (term("milling-service"), term("material-supply").model_copy(update={"version": "2"}),
                        term("material-supply").model_copy(update={"namespace": "other.example"})):
            self.assertEqual(self.store.search(base.model_copy(update={"profile": profile})).candidates, ())
        inputs = list(base.inputs)
        for value in (term("aluminum-7075"), "aluminum-6061", term("aluminum-6061").model_copy(update={"version": "2"})):
            changed = inputs[0].model_copy(update={"value": value})
            self.assertEqual(self.store.search(base.model_copy(update={"inputs": (changed, *inputs[1:])})).candidates, ())

    def test_same_offering_must_match_all_material_and_location_filters(self):
        shop = define_shop()
        other = shop.listings[0].model_copy(update={"id": "another", "profile": term("milling-service")})
        shop = shop.model_copy(update={"listings": (shop.listings[0].model_copy(update={"fields": shop.listings[0].fields[1:]}), other)})
        self.store.advertise(self.first, "supplier-far", "owner-a", self.advertisement(shop=shop))
        self.assertEqual(self.store.search(self.search()).candidates, ())

    def test_expiry_withdrawal_and_replayed_writes_cannot_resurrect_entries(self):
        ad = self.advertisement()
        receipt = self.store.advertise(self.first, "supplier-far", "owner-a", ad)
        self.assertEqual(self.store.advertise(self.first, "supplier-far", "owner-a", ad), receipt)
        self.now += timedelta(seconds=121)
        self.assertFalse(self.store.search(self.search()).candidates)
        self.store.advertise(self.first, "supplier-far", "owner-a", ad)
        self.assertFalse(self.store.search(self.search()).candidates)
        remove = Withdrawal(request_id=uuid.uuid4().hex, expected_version=1)
        removed = self.store.withdraw(self.first, "supplier-far", "owner-a", remove)
        self.assertIsNone(removed.expires_at)
        self.assertEqual(self.store.withdraw(self.first, "supplier-far", "owner-a", remove), removed)
        with self.assertRaises(Conflict):
            self.store.advertise(self.first, "supplier-far", "owner-a", ad)
        reopened = DirectoryStore(self.settings, clock=lambda: self.now)
        self.assertEqual(reopened.head(self.first, "supplier-far"), removed)
        self.assertFalse(reopened.search(self.search()).candidates)

    def test_stale_observations_generation_rollback_and_changed_request_keys_fail(self):
        ad = self.advertisement(generation=2)
        self.store.advertise(self.first, "supplier-far", "owner-a", ad)
        with self.assertRaises(Conflict):
            self.store.advertise(self.first, "supplier-far", "owner-a", ad.model_copy(update={"lease_seconds": 300}))
        with self.assertRaises(Conflict):
            self.store.advertise(self.first, "supplier-far", "owner-a", self.advertisement(version=1, generation=1))
        self.now += timedelta(seconds=31)
        with self.assertRaises(ValueError):
            self.store.advertise(self.first, "supplier-far", "owner-a", ad.model_copy(update={"request_id": uuid.uuid4().hex, "expected_version": 1}))
        self.assertEqual(self.store.head(self.first, "supplier-far").version, 1)

    def test_pagination_binds_the_snapshot_and_query(self):
        self.store.advertise(self.first, "supplier-near", "owner-a", self.advertisement("supplier-near"))
        self.store.advertise(self.second, "supplier-far", "owner-b", self.advertisement())
        search = self.search(limit=1)
        page = self.store.search(search)
        self.assertIsNotNone(page.next_cursor)
        following = self.store.search(search.model_copy(update={"cursor": page.next_cursor}))
        self.assertEqual(following.candidates[0].shop_id, "supplier-far")
        self.assertIsNone(following.next_cursor)
        with self.assertRaises(ValueError):
            self.store.search(search.model_copy(update={"cursor": page.next_cursor, "radius_m": 1}))
        self.store.withdraw(self.first, "supplier-near", "owner-a", Withdrawal(request_id=uuid.uuid4().hex, expected_version=1))
        with self.assertRaises(Conflict):
            self.store.search(search.model_copy(update={"cursor": page.next_cursor}))

    def test_opt_out_and_unknown_locations_are_not_silently_ranked_as_nearby(self):
        shop = define_shop().model_copy(update={"listings": (define_shop().listings[0].model_copy(update={"indexed": False}),)})
        receipt = self.store.advertise(self.first, "supplier-far", "owner-a", self.advertisement(shop=shop))
        self.assertEqual(receipt.indexed_listings, 0)
        self.assertFalse(self.store.search(self.search()).candidates)
        shop = define_shop().model_copy(update={"sites": (define_shop().sites[0].model_copy(update={"public_location": None}),)})
        self.store.advertise(self.first, "supplier-far", "owner-a", self.advertisement(shop=shop, version=1, generation=2))
        self.assertFalse(self.store.search(self.search()).candidates)
        candidate = self.store.search(self.search(include_unlocated=True)).candidates[0]
        self.assertIsNone(candidate.distance_m)

    def test_capacity_failure_rolls_back_the_whole_replacement(self):
        ad = self.advertisement()
        original = self.store.advertise(self.first, "supplier-far", "owner-a", ad)
        self.store.settings = self.settings.model_copy(update={"max_choices": 1})
        with self.assertRaises(DirectoryCapacity):
            self.store.advertise(self.first, "supplier-far", "owner-a", self.advertisement(version=1))
        self.assertEqual(self.store.head(self.first, "supplier-far"), original)
        self.assertEqual(len(self.store.search(self.search()).candidates), 1)

    def test_owner_revocation_removes_search_visibility_and_other_owner_cannot_replace(self):
        self.store.advertise(self.first, "supplier-far", "owner-a", self.advertisement())
        with self.assertRaises(Conflict):
            self.store.advertise(self.first, "supplier-far", "owner-b", self.advertisement(version=1))
        self.store.settings = self.settings.model_copy(update={"bindings": ()})
        self.assertFalse(self.store.search(self.search()).candidates)

    def test_search_supports_the_full_enrolled_owner_limit(self):
        self.store.advertise(self.first, "supplier-far", "owner-a", self.advertisement())
        others = tuple(OwnerBinding(subject=f"owner-{number}", instance_id=f"{number:032x}")
                       for number in range(1023))
        self.store.settings = self.settings.model_copy(update={
            "bindings": (self.settings.bindings[0], *others),
        })
        self.assertEqual([item.shop_id for item in self.store.search(self.search()).candidates], ["supplier-far"])

    def test_distance_handles_date_line_and_unknown_locations(self):
        self.assertLess(distance(0, 179.9, 0, -179.9), 23_000)
        self.assertEqual(distance(1, 1, 1, 1), 0)
        self.assertIsNone(distance(None, None, 0, 0))

    def test_heat_treatment_and_nonphysical_services_use_the_same_index(self):
        for profile, meaning in (("thermal", "heat-treatment-service"), ("review", "document-review")):
            self.store.advertise(self.first, profile, "owner-a", self.advertisement(profile))
            page = self.store.search(SupplierSearch(profile=term(meaning)))
            self.assertEqual([item.shop_id for item in page.candidates], [profile])
            self.assertIsNone(page.candidates[0].distance_m)

    def test_material_from_a_far_listing_cannot_be_combined_with_a_nearby_site(self):
        shop = define_shop()
        near = Site(id="near", name="Nearby site", visibility="public",
                    public_location=GeoPoint(latitude="41.88", longitude="-87.63", accuracy_m=100))
        incomplete = shop.listings[0].model_copy(update={"id": "nearby", "site": "near", "fields": shop.listings[0].fields[1:]})
        shop = shop.model_copy(update={"sites": (*shop.sites, near), "listings": (*shop.listings, incomplete)})
        self.store.advertise(self.first, "supplier-far", "owner-a", self.advertisement(shop=shop))
        self.assertFalse(self.store.search(self.search().model_copy(update={"radius_m": 1000})).candidates)
