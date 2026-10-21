"""Public listing evaluation shares REST, MCP and Python semantics."""

import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from program_support import call as mcp_call
from server_support import IdentityFixture

from chipload import BuildIdentity, compile_shop
from chipload.access import ACTIONS, Grant, Principal
from chipload.auth import TokenVerifier
from chipload.client import Client, route
from chipload.lifecycle import Repository, ShopService
from chipload.operations import Operations
from chipload.project import BuildReport, run_cases
from chipload.published import PublishedService
from chipload.server import create_app
from examples.federation_shops import AT, PROFILES, define_shop, project, query


class ListingTransportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        identity = IdentityFixture(directory.name)
        self.addCleanup(identity.client.close)
        self.owner = Principal(subject="owner", grants=tuple(Grant(shop_id=name, actions=ACTIONS) for name in PROFILES))
        self.service = ShopService(Repository(identity.settings.database))
        self.published = PublishedService(self.service, clock=lambda: AT)
        self.operations = Operations(self.service, self.published)
        app = create_app(identity.settings, operations=self.operations, verifier=TokenVerifier(identity.settings, client=identity.client))
        self.http = TestClient(app, base_url=identity.settings.base_url)
        self.http.__enter__()
        self.addCleanup(self.http.__exit__, None, None, None)
        self.sdk = Client(lambda name, body: self.operations.call(name, None, body))

    def publish(self, profile="supplier-far", shop=None):
        source = project(profile)
        self.service.save_source(self.owner, source, expected_head=None)
        job = self.service.request_build(self.owner, source.shop_id, source.revision)
        claim, _ = self.service.repository.claim(job.id, 60)
        artifact = compile_shop(shop or define_shop(profile), BuildIdentity(
            source_sha256=source.revision, sdk_sha256="a" * 64, dependencies_sha256="b" * 64,
            runtime="trusted-listing-transport-fixture",
        ))
        self.service.repository.finish(job.id, claim, BuildReport(
            source_revision=source.revision, worker_image="sha256:" + "c" * 64, status="succeeded",
            artifact=artifact, tests=run_cases(artifact, source.tests),
        ))
        self.service.publish(self.owner, source.shop_id, job.id, expected_generation=0)
        return self.sdk.shop(profile).catalog()

    def request(self, profile="supplier-far", **changes):
        revision = self.sdk.shop(profile).catalog()["public_sha256"]
        return {"shop_id": profile, "listing_id": "service",
                "query": query(profile, revision=revision, **changes).model_dump(mode="json")}

    def test_all_examples_evaluate_identically_with_business_rules_over_each_interface(self):
        for profile in PROFILES:
            catalog = self.publish(profile)
            self.assertEqual(catalog, self.http.get(f"/v1/shops/{profile}/catalog").json())
            self.assertEqual(self.http.get(f"/v1/shops/{profile}/listings/service").json()["item"], catalog["listings"][0])
            for count, documentation, expected in (("100", "complete", "supported"),
                                                    ("0", "complete", "unsupported"),
                                                    ("100", "incomplete", "conditional")):
                body = self.request(profile, count=count, documentation=documentation)
                method, path, data = route("listing_evaluate", body)
                response = self.http.request(method, path, json=data)
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                self.assertEqual(result["outcome"], expected)
                self.assertEqual(result, self.sdk.shop(profile).evaluate_listing("service", body["query"]))
                mcp = mcp_call(self.http, "listing_evaluate", body).json()["result"]
                self.assertFalse(mcp.get("isError"), mcp)
                self.assertEqual(result, mcp["structuredContent"])
                for private in ("model_sha256", "evidence", "observations", "steps", "assumptions"):
                    self.assertNotIn(private, result)

    def test_publication_guard_withdrawal_and_path_identities_are_enforced(self):
        self.publish()
        body = self.request()
        path = "/v1/shops/supplier-far/listings/service/evaluate"
        changed = {**body, "query": {**body["query"], "expected_revision": "0" * 64}}
        self.assertEqual(self.http.post(path, json=changed).status_code, 409)
        self.assertEqual(self.http.post(path.replace("service", "another"), json=body).status_code, 422)
        self.service.withdraw(self.owner, "supplier-far", expected_generation=1)
        self.assertEqual(self.http.post(path, json=body).status_code, 404)

    def test_private_and_missing_listings_are_indistinguishable(self):
        shop = define_shop()
        listing = shop.listings[0].model_copy(update={"id": "private", "visibility": "private", "indexed": False})
        self.publish(shop=shop.model_copy(update={"listings": (*shop.listings, listing)}))
        body = self.request()
        responses = []
        for identifier in ("private", "absent"):
            response = self.http.post(f"/v1/shops/supplier-far/listings/{identifier}/evaluate",
                                      json={**body, "listing_id": identifier})
            self.assertEqual(response.status_code, 404)
            responses.append(response.json())
        self.assertEqual(*responses)

    def test_unknown_binding_and_missing_availability_stay_unknown(self):
        self.publish()
        body = self.request()
        body["query"]["inputs"][0]["value"]["version"] = "99"
        result = self.sdk.shop("supplier-far").evaluate_listing("service", body["query"])
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(result["reasons"][0]["code"], "unmapped-listing-input")
        readiness = self.request(purpose="readiness")
        result = self.sdk.shop("supplier-far").evaluate_listing("service", readiness["query"])
        self.assertEqual(result["outcome"], "unknown")
        self.assertTrue(any(item["state"] == "missing" for item in result["freshness"]))

    def test_mapping_and_evaluation_use_one_publication_snapshot(self):
        self.publish()
        request = query(revision=self.sdk.shop("supplier-far").catalog()["public_sha256"])
        with patch.object(self.published, "_snapshot", wraps=self.published._snapshot) as snapshot:
            self.published.listing_evaluation("supplier-far", "service", request)
        self.assertEqual(snapshot.call_count, 1)

    def test_literal_input_cannot_probe_hidden_catalog_membership(self):
        shop = define_shop()
        listing = shop.listings[0]
        listing = listing.model_copy(update={"fields": (
            listing.fields[0].model_copy(update={"choices": ()}), *listing.fields[1:],
        )})
        hidden = shop.catalogs[0].model_copy(update={"visibility": "private"})
        self.publish(shop=shop.model_copy(update={"listings": (listing,), "catalogs": (hidden, *shop.catalogs[1:])}))
        results = []
        for guess in ("aluminum-6061", "aluminum-7075"):
            body = self.request()
            body["query"]["inputs"][0]["value"] = guess
            results.append(self.sdk.shop("supplier-far").evaluate_listing("service", body["query"]))
        self.assertEqual(*results)
        self.assertEqual(results[0]["outcome"], "unknown")
        self.assertEqual(results[0]["reasons"][0]["code"], "unpublished-condition")
