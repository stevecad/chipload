"""Every example's technical and commercial contracts through real REST/MCP adapters."""

import tempfile
import unittest

from fastapi.testclient import TestClient
from server_support import IdentityFixture

from chipload import BuildIdentity, compile_shop
from chipload.access import ACTIONS, Grant, Principal
from chipload.auth import TokenVerifier
from chipload.lifecycle import Repository, ShopService
from chipload.operations import Operations
from chipload.project import BuildReport, run_cases
from chipload.published import PublishedService
from chipload.server import create_app
from examples.process_shops import mixed
from examples.process_shops.definitions import AT
from examples.process_shops.export import project
from examples.process_shops.library import BY_ID
from examples.process_shops.shops import define_shop


class ProcessTransportTests(unittest.TestCase):
    def test_all_processes_and_business_rules_share_rest_mcp_results(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = IdentityFixture(directory)
            self.addCleanup(identity.client.close)
            owner = Principal(
                subject="owner",
                grants=tuple(
                    Grant(shop_id=project(id).shop_id, actions=ACTIONS) for id in (*BY_ID, "mixed")
                ),
            )
            settings = identity.settings.model_copy(update={"bindings": (owner,)})
            service = ShopService(Repository(settings.database))
            published = PublishedService(service, clock=lambda: AT)
            operations = Operations(service, published)
            verifier = TokenVerifier(settings, client=identity.client)
            app = create_app(settings, operations=operations, verifier=verifier)
            with TestClient(app, base_url=settings.base_url) as client:
                headers = {"Authorization": "Bearer " + identity.token()}
                for id in (*BY_ID, "mixed"):
                    with self.subTest(process=id):
                        source = project(id)
                        model = mixed.define_shop() if id == "mixed" else define_shop(BY_ID[id])
                        saved = service.save_source(owner, source, expected_head=None)
                        build = service.request_build(owner, source.shop_id, saved.revision)
                        claim, _ = service.repository.claim(build.id, 60)
                        artifact = compile_shop(
                            model,
                            BuildIdentity(
                                source_sha256=saved.revision,
                                sdk_sha256="a" * 64,
                                dependencies_sha256="b" * 64,
                                runtime="transport-test-fixture",
                            ),
                        )
                        # Trusted test fixture only. Real source execution is separately
                        # checked for every exported project in test_process_worker_live.
                        service.repository.finish(
                            build.id,
                            claim,
                            BuildReport(
                                source_revision=saved.revision,
                                worker_image="sha256:" + "c" * 64,
                                status="succeeded",
                                artifact=artifact,
                                tests=run_cases(artifact, source.tests),
                            ),
                        )
                        service.publish(owner, source.shop_id, build.id, expected_generation=0)
                        published.write_observations(
                            owner,
                            source.shop_id,
                            mixed.observations() if id == "mixed" else BY_ID[id].observations(),
                            expected_revision=None,
                        )
                        base = f"/v1/shops/{source.shop_id}"
                        catalog = client.get(base + "/catalog")
                        self.assertEqual(catalog.status_code, 200, catalog.text)
                        self.assertEqual(
                            catalog.json(),
                            self.mcp(client, "shop_catalog", {"shop_id": source.shop_id}),
                        )
                        self.assertNotIn("private-equipment.example.invalid", catalog.text)
                        for case in source.tests:
                            if id != "mixed" and case.id not in {
                                "technical-supported",
                                "process-specific-rejection",
                                "production-below-minimum",
                                "prototype-exception",
                                "fractional-part-count",
                                "incoming-paperwork-needed",
                                "customer-requirements-unreviewed",
                            }:
                                continue
                            body = {
                                "shop_id": source.shop_id,
                                "query": case.request.model_dump(mode="json"),
                            }
                            # The deliberate invalid route is private. Exercise the
                            # authorized detail interface for the complete mixed matrix.
                            private = id == "mixed"
                            response = client.post(
                                base + ("/private/evaluate" if private else "/evaluate"),
                                json=body,
                                headers=headers if private else {},
                            )
                            self.assertEqual(response.status_code, 200, response.text)
                            other = self.mcp(
                                client,
                                "shop_evaluate_private" if private else "shop_evaluate",
                                body,
                                headers if private else None,
                            )
                            self.assertEqual(response.json(), other)
                            self.assertEqual(
                                response.json()["outcome"], case.expected, f"{id}/{case.id}"
                            )
                        query = {
                            "shop_id": source.shop_id,
                            "query": {
                                "ids": [
                                    "minimum-production-lot",
                                    "typical-turnaround",
                                    "minimum-charge",
                                ],
                                "context": [{"name": "engagement", "value": "prototype"}],
                            },
                        }
                        response = client.post(base + "/variables/query", json=query)
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual(response.json(), self.mcp(client, "shop_variables", query))
                        findings = {v["definition"]["id"]: v for v in response.json()["variables"]}
                        self.assertEqual(
                            findings["minimum-production-lot"]["applicability"], "not-applicable"
                        )
                        self.assertEqual(
                            findings["typical-turnaround"]["definition"]["certainty"], "typical"
                        )
                        self.assertIsNone(findings["minimum-charge"]["effective_value"])

    def mcp(self, client, name, arguments, headers=None):
        response = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "tools/call",
                "Mcp-Name": name,
                **(headers or {}),
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": name,
                    "arguments": {"request": arguments},
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
                    },
                },
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json().get("result")
        self.assertIsNotNone(result, response.text)
        self.assertFalse(result.get("isError"), result)
        return result["structuredContent"]
