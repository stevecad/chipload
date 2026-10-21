"""REST/MCP offer parity, publication selection and hidden-route privacy."""

import json
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from program_support import call as program_call
from server_support import IdentityFixture

from chipload import BuildIdentity, Catalog, CatalogItem, compile_shop
from chipload.access import ACTIONS, Grant, Principal
from chipload.auth import TokenVerifier
from chipload.lifecycle import Repository, ShopService
from chipload.operations import Operations
from chipload.project import BuildReport, run_cases
from chipload.published import PublishedService
from chipload.quotas import QuotaPolicy
from chipload.server import create_app
from examples.alternative_routes import AT, PROFILES, cases, define_shop, project


class OfferTransportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.identity = IdentityFixture(directory.name)
        self.addCleanup(self.identity.client.close)
        self.owner = Principal(
            subject="owner",
            grants=tuple(Grant(shop_id="routes-" + name, actions=ACTIONS) for name in PROFILES),
        )
        self.settings = self.identity.settings.model_copy(update={"bindings": (self.owner,)})
        self.service = ShopService(Repository(self.settings.database))
        limits = self.service.repository.read_quotas()
        for scope in ("deployment", "peer", "identity"):
            limits["policy"]["requests"][scope] = {"per_minute": 6000, "burst": 1000}
        self.service.repository.set_quotas(
            QuotaPolicy.model_validate(limits["policy"]), expected_generation=limits["generation"]
        )
        self.published = PublishedService(self.service, clock=lambda: AT)
        self.operations = Operations(self.service, self.published)
        app = create_app(
            self.settings,
            operations=self.operations,
            verifier=TokenVerifier(self.settings, client=self.identity.client),
        )
        self.client = TestClient(app, base_url=self.settings.base_url)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.headers = {"Authorization": "Bearer " + self.identity.token()}

    def publish(self, shop, profile="machining", generation=0):
        source = project(profile)
        if generation == 0:
            self.service.save_source(self.owner, source, expected_head=None)
        job = self.service.request_build(self.owner, shop.id, source.revision)
        claim, _ = self.service.repository.claim(job.id, 60)
        artifact = compile_shop(
            shop,
            BuildIdentity(
                source_sha256=source.revision,
                sdk_sha256="a" * 64,
                dependencies_sha256="b" * 64,
                runtime="trusted-transport-fixture",
            ),
        )
        # Domain/transport fixture. The actual exported source runs in the opt-in worker test.
        self.service.repository.finish(
            job.id,
            claim,
            BuildReport(
                source_revision=source.revision,
                worker_image="sha256:" + "c" * 64,
                status="succeeded",
                artifact=artifact,
                tests=run_cases(artifact, source.tests),
            ),
        )
        self.service.publish(self.owner, shop.id, job.id, expected_generation=generation)
        return job

    def mcp(self, operation, body):
        response = program_call(self.client, operation, body)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertFalse(result.get("isError"), result)
        return result["structuredContent"]

    def test_all_examples_have_equivalent_catalogs_and_evaluations(self):
        for profile in PROFILES:
            shop = define_shop(profile)
            self.publish(shop, profile)
            base = f"/v1/shops/{shop.id}"
            catalog = self.client.get(base + "/catalog")
            self.assertEqual(catalog.status_code, 200, catalog.text)
            self.assertEqual(catalog.json(), self.mcp("shop_catalog", {"shop_id": shop.id}))
            page = self.client.get(base + "/offers").json()
            self.assertEqual(
                page, self.mcp("shop_list", {"shop_id": shop.id, "collection": "offers"})
            )
            self.assertEqual(page["items"][0]["id"], "service")
            item = self.client.get(base + "/offers/service").json()
            self.assertEqual(
                item,
                self.mcp(
                    "shop_inspect",
                    {"shop_id": shop.id, "collection": "offers", "item_id": "service"},
                ),
            )
            for case in cases(profile):
                with self.subTest(profile=profile, case=case.id):
                    body = {"shop_id": shop.id, "query": case.request.model_dump(mode="json")}
                    response = self.client.post(base + "/evaluate", json=body)
                    self.assertEqual(response.status_code, 200, response.text)
                    data = response.json()
                    self.assertEqual(data, self.mcp("shop_evaluate", body))
                    self.assertEqual(data["outcome"], case.expected)
                    self.assertEqual(len(data["alternatives"]), 2)
                    for private in ("model_sha256", "evidence", "assumptions", "steps", "path"):
                        self.assertNotIn('"' + private + '"', json.dumps(data))

    def test_private_route_and_nested_private_step_block_public_evaluation(self):
        for private_step in (False, True):
            profile = "thermal" if private_step else "machining"
            shop = define_shop(profile)
            if private_step:
                shop = shop.model_copy(
                    update={
                        "capabilities": tuple(
                            cap.model_copy(update={"visibility": "private", "name": "SECRET STEP"})
                            if cap.id == "inspection"
                            else cap
                            for cap in shop.capabilities
                        )
                    }
                )
            else:
                shop = shop.model_copy(
                    update={
                        "workflows": tuple(
                            route.model_copy(
                                update={"visibility": "private", "name": "SECRET ROUTE"}
                            )
                            if route.id == "contract-route"
                            else route
                            for route in shop.workflows
                        )
                    }
                )
            self.publish(shop, profile)
            body = {"shop_id": shop.id, "query": cases(profile)[0].request.model_dump(mode="json")}
            base = f"/v1/shops/{shop.id}"
            with patch(
                "chipload.published.evaluate", side_effect=AssertionError("opaque model evaluated")
            ):
                response = self.client.post(base + "/evaluate", json=body)
                self.assertEqual(response.json(), self.mcp("shop_evaluate", body))
            self.assertEqual(response.json()["outcome"], "unknown")
            self.assertEqual(response.json()["reasons"][0]["code"], "unpublished-condition")
            self.assertNotIn("alternatives", response.json())
            self.assertNotIn("SECRET", self.client.get(base + "/catalog").text)
            private = self.client.post(base + "/private/evaluate", json=body, headers=self.headers)
            self.assertEqual(private.status_code, 200, private.text)
            self.assertEqual(private.json()["outcome"], "supported")
            denied = self.client.post(
                base + "/private/evaluate",
                json=body,
                headers={"Authorization": "Bearer " + self.identity.token(subject="outsider")},
            )
            self.assertEqual(denied.status_code, 403)

    def test_hidden_catalog_membership_in_a_route_is_never_probed_publicly(self):
        shop = define_shop()
        route = shop.workflows[0]
        hidden = route.requirements[0].model_copy(update={"catalog": "contracts"})
        shop = shop.model_copy(
            update={
                "catalogs": (
                    Catalog(
                        id="contracts",
                        name="SECRET CONTRACTS",
                        items=(CatalogItem(id="prototype", name="Secret customer"),),
                    ),
                ),
                "workflows": (
                    route.model_copy(update={"requirements": (hidden, *route.requirements[1:])}),
                    shop.workflows[1],
                ),
            }
        )
        self.publish(shop)
        body = {"shop_id": shop.id, "query": cases("machining")[0].request.model_dump(mode="json")}
        answers = []
        with patch(
            "chipload.published.evaluate", side_effect=AssertionError("membership evaluated")
        ):
            for value in ("prototype", "production", "not-a-customer"):
                body["query"]["facts"][1]["value"] = value
                response = self.client.post(f"/v1/shops/{shop.id}/evaluate", json=body)
                answers.append(response.json())
        self.assertEqual(answers[0], answers[1])
        self.assertEqual(answers[1], answers[2])
        self.assertEqual(answers[0]["outcome"], "unknown")

    def test_publication_withdrawal_and_rollback_control_offer_visibility(self):
        shop = define_shop()
        original = self.publish(shop)
        self.publish(
            shop.model_copy(
                update={"offers": (shop.offers[0].model_copy(update={"name": "Updated offer"}),)}
            ),
            generation=1,
        )
        base = f"/v1/shops/{shop.id}"
        self.assertEqual(
            self.client.get(base + "/offers/service").json()["item"]["name"], "Updated offer"
        )
        self.service.publish(self.owner, shop.id, original.id, expected_generation=2)
        self.assertEqual(
            self.client.get(base + "/offers/service").json()["item"]["name"], "Shop service"
        )
        self.service.withdraw(self.owner, shop.id, expected_generation=3)
        self.assertEqual(self.client.get(base + "/offers").status_code, 404)


if __name__ == "__main__":
    unittest.main()
