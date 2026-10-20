"""Rate limits protect both transports before expensive work and share mutation budgets."""

import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from server_support import IdentityFixture

from chipload.quotas import QuotaPolicy
from chipload.server import create_app
from examples.export_project import project

META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


class RequestLimitTransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.identity = IdentityFixture(temporary.name)
        self.addCleanup(self.identity.client.close)
        self.operations = self.identity.operations()
        self.operations.limiter.clock = lambda: 1_792_454_400_000
        app = create_app(
            self.identity.settings, operations=self.operations, verifier=self.identity.verifier
        )
        self.client = TestClient(
            app, base_url=self.identity.settings.base_url, client=("198.51.100.1", 50000)
        )
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.owner = {"Authorization": "Bearer " + self.identity.token()}

    def policy(self, **changes):
        repository = self.operations.service.repository
        status = repository.read_quotas()
        for scope, values in changes.items():
            status["policy"]["requests"][scope].update(values)
        repository.set_quotas(
            QuotaPolicy.model_validate(status["policy"]), expected_generation=status["generation"]
        )

    def mcp(self, name, arguments, headers=None):
        return self.client.post(
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
                "params": {"name": name, "arguments": {"request": arguments}, "_meta": META},
            },
        )

    def test_ingress_rejects_before_token_verification_body_parsing_or_domain_calls(self):
        self.policy(peer={"burst": 1})
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        with (
            patch.object(self.identity.verifier, "verify", side_effect=AssertionError("verified")),
            patch("chipload.server.unique_json", side_effect=AssertionError("decoded")),
            patch.object(self.operations, "call", side_effect=AssertionError("executed")),
        ):
            rest = self.client.put(
                "/v1/shops/thermal-example/source",
                content=b"not JSON",
                headers={**self.owner, "Content-Type": "application/json"},
            )
            mcp = self.mcp("source_save", {}, self.owner)
        self.assertEqual(rest.status_code, 429)
        self.assertEqual(mcp.status_code, 429)
        self.assertEqual(mcp.json()["error"]["data"], {"status": 429, **rest.json()})
        self.assertIsNone(mcp.json()["id"])
        self.assertEqual(rest.json()["error"]["limit"], "peer")
        self.assertEqual(rest.headers["Retry-After"], "1")
        self.assertEqual(rest.headers["Cache-Control"], "no-store")

    def test_forwarded_headers_cannot_reset_the_peer_budget(self):
        self.policy(peer={"burst": 1})
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        for address in ("198.51.100.2", "198.51.100.3"):
            response = self.client.get(
                "/healthz",
                headers={
                    "X-Forwarded-For": address,
                    "Forwarded": f"for={address}",
                    "X-Real-IP": address,
                },
            )
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.json()["error"]["limit"], "peer")

    def test_new_tokens_for_same_identity_and_different_transports_share_a_budget(self):
        self.policy(identity={"burst": 1})
        self.assertEqual(self.client.get("/v1/identity", headers=self.owner).status_code, 200)
        refreshed = {"Authorization": "Bearer " + self.identity.token(changes={"jti": "new-token"})}
        response = self.mcp("identity_read", {}, refreshed)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["data"]["error"]["limit"], "identity")
        self.assertEqual(self.client.get("/v1/shops/thermal-example/catalog").status_code, 200)

    def test_mutation_rejections_preserve_source_and_publication_across_rest_and_mcp(self):
        self.policy(mutation_identity={"burst": 1})
        source = project()
        body = {"shop_id": source.shop_id, "revision": source.revision, "request_key": "one"}
        first = self.client.post("/v1/shops/thermal-example/builds", json=body, headers=self.owner)
        self.assertEqual(first.status_code, 202)
        changed = source.model_dump(mode="json")
        changed["files"][0]["content"] += "\n# never saved\n"
        write = {"project": changed, "expected_head": source.revision}
        rest = self.client.put("/v1/shops/thermal-example/source", json=write, headers=self.owner)
        mcp = self.mcp("source_save", write, self.owner).json()["result"]
        self.assertEqual(rest.status_code, 429)
        self.assertEqual(rest.json()["error"]["limit"], "mutation_identity")
        self.assertEqual(mcp["structuredContent"], {"status": 429, **rest.json()})
        self.assertTrue(mcp["isError"])
        saved = self.client.get("/v1/shops/thermal-example/source", headers=self.owner).json()
        self.assertEqual(saved["revision"], source.revision)
        self.assertEqual(self.client.get("/v1/shops/thermal-example/catalog").status_code, 200)

    def test_forbidden_mutations_cannot_spend_another_shops_allowance(self):
        self.policy(mutation_shop={"burst": 1})
        write = {"project": project().model_dump(mode="json"), "expected_head": project().revision}
        unbound = {"Authorization": "Bearer " + self.identity.token("unbound")}
        denied = self.client.put("/v1/shops/thermal-example/source", json=write, headers=unbound)
        self.assertEqual(denied.status_code, 403)
        saved = self.client.put("/v1/shops/thermal-example/source", json=write, headers=self.owner)
        self.assertEqual(saved.status_code, 200)

    def test_database_contention_returns_retryable_http_errors_before_either_protocol(self):
        with self.operations.service.repository.admission.transaction():
            rest = self.client.get("/v1/shops/thermal-example/catalog")
            mcp = self.mcp("shop_catalog", {"shop_id": "thermal-example"})
        self.assertEqual(rest.status_code, 503)
        self.assertEqual(mcp.status_code, 503)
        self.assertEqual(mcp.json()["error"]["data"], {"status": 503, **rest.json()})
        self.assertEqual(rest.json()["error"]["code"], "request_capacity")
        self.assertEqual(rest.headers["Retry-After"], "1")
        self.assertEqual(self.client.get("/v1/shops/thermal-example/catalog").status_code, 200)

    def test_shop_write_lock_preserves_public_rest_and_mcp_reads(self):
        with self.operations.service.repository.transaction() as db:
            db.execute("UPDATE shops SET head=head WHERE id='thermal-example'")
            rest = self.client.get("/v1/shops/thermal-example/catalog")
            mcp = self.mcp("shop_catalog", {"shop_id": "thermal-example"})
        self.assertEqual(rest.status_code, 200, rest.text)
        self.assertEqual(mcp.status_code, 200, mcp.text)
        self.assertEqual(mcp.json()["result"]["structuredContent"], rest.json())

    def test_allowed_browser_origin_can_read_rate_errors_and_retry_header(self):
        self.policy(peer={"burst": 1})
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        response = self.client.get("/healthz", headers={"Origin": self.identity.settings.base_url})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(
            response.headers["Access-Control-Allow-Origin"], self.identity.settings.base_url
        )
        self.assertIn("Retry-After", response.headers["Access-Control-Expose-Headers"])


if __name__ == "__main__":
    unittest.main()
