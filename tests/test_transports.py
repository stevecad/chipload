import json
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from program_support import call as program_call
from program_support import processor
from server_support import IdentityFixture

from chipload.cache import DataBusy
from chipload.quotas import QuotaPolicy
from chipload.server import MAX_REQUEST_BYTES, create_app
from examples.export_project import project
from examples.heat_treatment import SCENARIOS

META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


class TransportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.identity = IdentityFixture(directory.name)
        self.addCleanup(self.identity.client.close)
        self.operations = self.identity.operations()
        app = create_app(
            self.identity.settings, operations=self.operations, verifier=self.identity.verifier
        )
        self.client = TestClient(app, base_url=self.identity.settings.base_url)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.owner = {"Authorization": "Bearer " + self.identity.token()}

    def mcp(self, name, arguments, headers=None):
        return program_call(self.client, name, arguments, headers, worker=processor(self.operations, self.identity.settings))

    def structured(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertIn("result", payload, payload)
        self.assertFalse(payload["result"].get("isError"), payload)
        return payload["result"]["structuredContent"]

    def test_json_complexity_rejections_leave_both_transports_usable(self):
        before = self.client.get("/v1/shops/thermal-example/catalog").json()
        payloads = (
            b'{"value":' + b"[" * 33 + b"0" + b"]" * 33 + b"}",
            b"[" + b"0," * 100_000 + b"0]",
            b'{"value":NaN}',
        )
        for path in ("/v1/shops/thermal-example/evaluate", "/mcp"):
            for payload in payloads:
                with self.subTest(path=path, bytes=len(payload)):
                    response = self.client.post(
                        path, content=payload, headers={"Content-Type": "application/json"}
                    )
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertEqual(response.json(), {"error": {"code": "invalid_json"}})
        self.assertEqual(self.client.get("/v1/shops/thermal-example/catalog").json(), before)
        self.assertEqual(
            self.structured(self.mcp("shop_catalog", {"shop_id": "thermal-example"})), before
        )

    def test_data_load_capacity_is_retryable_on_rest_and_mcp(self):
        with patch.object(
            self.operations.published,
            "catalog",
            side_effect=DataBusy("data loading is at capacity; retry later"),
        ):
            rest = self.client.get("/v1/shops/thermal-example/catalog")
            mcp = self.mcp("shop_catalog", {"shop_id": "thermal-example"}).json()["result"]
        self.assertEqual(rest.status_code, 503)
        self.assertEqual(rest.json()["error"]["code"], "data_busy")
        self.assertTrue(mcp["isError"])
        self.assertEqual(mcp["structuredContent"], {"status": 503, **rest.json()})
        self.assertEqual(self.client.get("/v1/shops/thermal-example/catalog").status_code, 200)

    def test_build_quota_and_idempotent_retry_agree_on_rest_and_mcp(self):
        repository = self.operations.service.repository
        limits = repository.read_quotas()
        limits["policy"]["shop"]["pending_builds"] = 1
        repository.set_quotas(
            QuotaPolicy.model_validate(limits["policy"]), expected_generation=limits["generation"]
        )
        source = self.client.get("/v1/shops/thermal-example/source", headers=self.owner).json()
        body = {"shop_id": "thermal-example", "revision": source["revision"], "request_key": "one"}
        path = "/v1/shops/thermal-example/builds"
        first = self.client.post(path, json=body, headers=self.owner)
        self.assertEqual(first.status_code, 202, first.text)
        denied = self.client.post(path, json={**body, "request_key": "two"}, headers=self.owner)
        self.assertEqual(denied.status_code, 429, denied.text)
        self.assertEqual(denied.headers["Retry-After"], "5")
        result = self.mcp("build_request", {**body, "request_key": "two"}, self.owner).json()[
            "result"
        ]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"], {"status": 429, **denied.json()})
        retried = self.structured(self.mcp("build_request", body, self.owner))
        self.assertEqual(retried["id"], first.json()["id"])

    def test_public_catalog_and_resources_need_no_agent_or_login(self):
        rest = self.client.get("/v1/shops/thermal-example/catalog")
        self.assertEqual(rest.status_code, 200)
        mcp = self.structured(self.mcp("shop_catalog", {"shop_id": "thermal-example"}))
        self.assertEqual(rest.json(), mcp)
        for forbidden in (
            "model_sha256",
            "source_sha256",
            "furnace-calibration",
            "thermal-declaration",
        ):
            self.assertNotIn(forbidden, rest.text)
        self.assertEqual(rest.headers["x-content-type-options"], "nosniff")
        resource = self.client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "resources/read",
                "Mcp-Name": "chipload://shops/thermal-example/catalog",
            },
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": "chipload://shops/thermal-example/catalog", "_meta": META},
            },
        )
        self.assertEqual(resource.status_code, 200, resource.text)
        self.assertEqual(json.loads(resource.json()["result"]["contents"][0]["text"]), rest.json())

    def test_all_heat_treatment_scenarios_have_rest_mcp_domain_parity(self):
        for scenario in SCENARIOS:
            body = {
                "shop_id": "thermal-example",
                "query": scenario.question.model_dump(mode="json"),
            }
            with self.subTest(scenario=scenario.name):
                rest = self.client.post(
                    "/v1/shops/thermal-example/private/evaluate", json=body, headers=self.owner
                )
                self.assertEqual(rest.status_code, 200, rest.text)
                mcp = self.structured(self.mcp("shop_evaluate_private", body, self.owner))
                self.assertEqual(rest.json(), mcp)
                self.assertEqual(rest.json()["outcome"], scenario.expected)

    def test_machining_scenarios_use_the_same_rest_and_mcp_evaluator(self):
        # One admitted program per scenario here isolates parity failures; the
        # composed workflow is covered by live tests. Admit the whole matrix
        # without depending on elapsed time to refill the request buckets.
        repository = self.operations.service.repository
        quota = repository.read_quotas()
        for scope in ("peer", "identity", "mutation_identity", "mutation_shop"):
            quota['policy']['requests'][scope]['burst'] = 100
        repository.set_quotas(QuotaPolicy.model_validate(quota['policy']), expected_generation=quota['generation'])
        from chipload.artifact import BuildIdentity, compile_shop
        from chipload.project import (
            AcceptanceCase,
            BuildReport,
            SourceFile,
            SourceProject,
            run_cases,
        )
        from examples.reference_shop.scenarios import AT
        from examples.reference_shop.scenarios import SCENARIOS as MACHINING
        from examples.reference_shop.shop import define_shop

        model = define_shop().model_copy(update={"id": "thermal-example"})
        source = SourceProject(
            shop_id="thermal-example",
            entrypoint="shop:define_shop",
            files=(
                SourceFile(
                    path="shop.py",
                    content=(
                        "from chipload import Shop\n"
                        f"def define_shop(): return Shop.model_validate_json({model.model_dump_json()!r})\n"
                    ),
                ),
            ),
            tests=tuple(
                AcceptanceCase(
                    id=case.name,
                    request=case.question,
                    at=AT,
                    observations=case.observation_snapshot,
                    expected=case.expected,
                    reason_codes=(case.reason,),
                )
                for case in MACHINING
            ),
        )
        service = self.operations.service
        owner = self.identity.settings.bindings[0]
        saved = service.save_source(owner, source, expected_head=project().revision)
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
        service.publish(owner, source.shop_id, build.id, expected_generation=1)
        for scenario in MACHINING:
            current = self.operations.published.read_observations(owner, source.shop_id)
            self.operations.published.write_observations(
                owner,
                source.shop_id,
                scenario.observation_snapshot,
                expected_revision=current.revision,
            )
            body = {"shop_id": source.shop_id, "query": scenario.question.model_dump(mode="json")}
            with self.subTest(scenario=scenario.name):
                rest = self.client.post(
                    "/v1/shops/thermal-example/private/evaluate", json=body, headers=self.owner
                )
                self.assertEqual(rest.status_code, 200, rest.text)
                self.assertEqual(
                    rest.json(),
                    self.structured(self.mcp("shop_evaluate_private", body, self.owner)),
                )
                self.assertEqual(rest.json()["outcome"], scenario.expected)

    def test_public_evaluation_contains_explanations_without_private_trace(self):
        body = {
            "shop_id": "thermal-example",
            "query": SCENARIOS[0].question.model_dump(mode="json"),
        }
        rest = self.client.post("/v1/shops/thermal-example/evaluate", json=body)
        self.assertEqual(rest.status_code, 200, rest.text)
        self.assertEqual(rest.json()["outcome"], "supported")
        self.assertEqual(rest.json(), self.structured(self.mcp("shop_evaluate", body)))
        for forbidden in (
            "thermal-declaration",
            "furnace-calibration",
            "model_sha256",
            "steps",
            "assumptions",
        ):
            self.assertNotIn(forbidden, rest.text)
        self.assertTrue(rest.json()["freshness"])
        body["query"] = SCENARIOS[-1].question.model_dump(mode="json")
        self.assertEqual(
            self.client.post("/v1/shops/thermal-example/evaluate", json=body).status_code, 404
        )

    def test_private_endpoints_and_tools_require_auth_and_shop_permission(self):
        path = "/v1/shops/thermal-example/source"
        rest = self.client.get(path)
        self.assertEqual(rest.status_code, 401)
        self.assertIn(self.identity.settings.metadata_url, rest.headers["www-authenticate"])
        mcp = self.mcp("source_read", {"shop_id": "thermal-example"})
        self.assertEqual(mcp.status_code, 401)
        stranger = {"Authorization": "Bearer " + self.identity.token("unbound")}
        self.assertEqual(self.client.get(path, headers=stranger).status_code, 403)
        mcp = self.mcp("source_read", {"shop_id": "thermal-example"}, stranger)
        self.assertTrue(mcp.json()["result"]["isError"], mcp.text)
        self.assertEqual(mcp.json()["result"]["structuredContent"]["status"], 403)
        self.assertEqual(self.client.get(path, headers=self.owner).status_code, 200)

    def test_source_guards_build_retry_and_permissions_match(self):
        saved = self.client.get("/v1/shops/thermal-example/source", headers=self.owner).json()
        self.assertEqual(
            saved,
            self.structured(self.mcp("source_read", {"shop_id": "thermal-example"}, self.owner)),
        )
        changed = project().model_dump(mode="json")
        changed["files"][0]["content"] += "\n# updated through REST\n"
        write = {"project": changed, "expected_head": saved["revision"]}
        updated = self.client.put(
            "/v1/shops/thermal-example/source", headers=self.owner, json=write
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        stale = self.mcp("source_save", write, self.owner).json()["result"]
        self.assertTrue(stale["isError"])
        self.assertEqual(stale["structuredContent"]["status"], 409)
        body = {
            "shop_id": "thermal-example",
            "revision": updated.json()["revision"],
            "request_key": "same-build",
        }
        with patch(
            "subprocess.run", side_effect=AssertionError("API must not execute source or Docker")
        ):
            queued = self.client.post(
                "/v1/shops/thermal-example/builds", json=body, headers=self.owner
            )
            retried = self.structured(self.mcp("build_request", body, self.owner))
        self.assertEqual(queued.status_code, 202, queued.text)
        self.assertEqual(queued.json()["id"], retried["id"])
        self.assertEqual(retried["state"], "queued")
        editor = {"Authorization": "Bearer " + self.identity.token("editor")}
        change = {"shop_id": "thermal-example", "action": "withdraw", "expected_generation": 1}
        self.assertEqual(
            self.client.put(
                "/v1/shops/thermal-example/publication", json=change, headers=editor
            ).status_code,
            403,
        )

    def test_filter_pagination_etag_and_withdrawal(self):
        path = "/v1/shops/thermal-example/capabilities"
        one = self.client.get(path, params={"limit": 1}).json()
        two = self.client.get(path, params={"limit": 1, "cursor": one["next_cursor"]}).json()
        self.assertNotEqual(one["items"][0]["id"], two["items"][0]["id"])
        self.assertEqual(len(self.client.get(path, params={"query": "thermal"}).json()["items"]), 1)
        self.assertEqual(self.client.get(path, params={"limit": 101}).status_code, 422)
        self.assertEqual(self.client.get(path, params={"cursor": "garbage"}).status_code, 422)
        catalog = self.client.get("/v1/shops/thermal-example/catalog")
        etag = catalog.headers["etag"]
        self.assertEqual(
            self.client.get(
                "/v1/shops/thermal-example/catalog", headers={"If-None-Match": etag}
            ).status_code,
            304,
        )
        self.client.put(
            "/v1/shops/thermal-example/publication",
            headers=self.owner,
            json={
                "shop_id": "thermal-example",
                "action": "withdraw",
                "expected_generation": 1,
            },
        ).raise_for_status()
        self.assertEqual(
            self.client.get(
                "/v1/shops/thermal-example/catalog", headers={"If-None-Match": etag}
            ).status_code,
            404,
        )

    def test_observation_conflicts_staleness_and_variables(self):
        path = "/v1/shops/thermal-example/observations"
        snapshot = self.client.get(path, headers=self.owner).json()
        self.assertEqual(
            snapshot,
            self.structured(
                self.mcp("observations_read", {"shop_id": "thermal-example"}, self.owner)
            ),
        )
        stale = snapshot["observations"]
        stale[0]["time"] = "2026-10-19T12:00:00Z"
        body = {
            "shop_id": "thermal-example",
            "expected_revision": snapshot["revision"],
            "observations": stale,
        }
        self.assertEqual(self.client.put(path, json=body, headers=self.owner).status_code, 200)
        self.assertEqual(self.client.put(path, json=body, headers=self.owner).status_code, 409)
        result = self.client.post(
            "/v1/shops/thermal-example/evaluate",
            json={
                "shop_id": "thermal-example",
                "query": SCENARIOS[0].question.model_dump(mode="json"),
            },
        ).json()
        self.assertEqual(result["outcome"], "unknown")
        self.assertIn("stale", {f["state"] for f in result["freshness"]})
        query = {"shop_id": "thermal-example", "query": {"ids": ["customer-qualification"]}}
        rest = self.client.post("/v1/shops/thermal-example/variables/query", json=query)
        self.assertEqual(rest.json(), self.structured(self.mcp("shop_variables", query)))
        self.assertIsNone(rest.json()["variables"][0]["effective_value"])

    def test_host_origin_json_size_and_invalid_token_checks(self):
        for headers, expected in (
            ({"Host": "attacker.example"}, 400),
            ({"Origin": "https://attacker.example"}, 403),
            ({"Authorization": "Bearer invalid"}, 401),
        ):
            self.assertEqual(self.client.get("/healthz", headers=headers).status_code, expected)
        self.assertEqual(
            self.client.post(
                "/mcp", content=b'{"id":1,"id":2}', headers={"Content-Type": "application/json"}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post("/mcp", content=b"x" * (MAX_REQUEST_BYTES + 1)).status_code, 413
        )
        self.assertEqual(self.client.get("/healthz?access_token=not-a-token").status_code, 400)
        metadata = self.client.get("/.well-known/oauth-protected-resource/mcp").json()
        self.assertEqual(metadata["resource"], self.identity.settings.resource_url)
        self.assertEqual(metadata["authorization_servers"], [self.identity.settings.issuer])

    def test_client_cannot_override_service_clock_or_inject_principal(self):
        body = {
            "shop_id": "thermal-example",
            "query": SCENARIOS[0].question.model_dump(mode="json"),
            "at": "2000-01-01T00:00:00Z",
            "principal": {"subject": "owner"},
        }
        result = self.client.post("/v1/shops/thermal-example/evaluate", json=body)
        self.assertEqual(result.status_code, 422)
        self.assertNotIn("input", result.json()["error"]["fields"][0])


if __name__ == "__main__":
    unittest.main()
