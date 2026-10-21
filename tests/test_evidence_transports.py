"""REST and MCP/Python share revocation permissions and private receipts."""

import tempfile
import unittest

from fastapi.testclient import TestClient
from program_support import call, processor
from server_support import IdentityFixture

from chipload.client import Client, route
from chipload.server import create_app
from examples.heat_treatment import request


class EvidenceTransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.identity = IdentityFixture(temporary.name)
        self.addCleanup(self.identity.client.close)
        self.operations = self.identity.operations()
        self.worker = processor(self.operations, self.identity.settings)
        self.client = TestClient(
            create_app(
                self.identity.settings, operations=self.operations, verifier=self.identity.verifier
            ),
            base_url=self.identity.settings.base_url,
        )
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.headers = {"Authorization": "Bearer " + self.identity.token()}
        self.base = "/v1/shops/thermal-example"
        model = self.client.get(self.base + "/evidence", headers=self.headers).json()["revision"]
        self.body = {
            "shop_id": "thermal-example",
            "evidence_id": "thermal-declaration",
            "expected_model_sha256": model,
            "reason": "Private qualification report",
        }
        self.path = self.base + "/evidence/thermal-declaration/revocation"

    def mcp(self, name, body):
        response = call(self.client, name, body, self.headers, worker=self.worker)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertFalse(result.get("isError"), result)
        return result["structuredContent"]

    def test_rest_and_mcp_python_share_one_receipt_and_public_result(self):
        receipt = self.client.put(self.path, json=self.body, headers=self.headers)
        self.assertEqual(receipt.status_code, 200, receipt.text)
        self.assertEqual(receipt.json(), self.mcp("evidence_revoke", self.body))
        reference = {key: self.body[key] for key in ("shop_id", "evidence_id")}
        self.assertEqual(
            self.client.get(self.path, headers=self.headers).json(),
            self.mcp("evidence_revocation_read", reference),
        )
        body = {"shop_id": "thermal-example", "query": request().model_dump(mode="json")}
        result = self.client.post(self.base + "/evaluate", json=body)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json(), self.mcp("shop_evaluate", body))
        self.assertEqual(result.json()["outcome"], "unknown")
        self.assertNotIn("Private qualification", result.text)

        # The ordinary HTTP Python client uses the same endpoints as hosted code.
        def dispatch(operation, body):
            method, path, payload = route(operation, body)
            response = self.client.request(method, path, json=payload, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

        shop = Client(dispatch).shop("thermal-example")
        self.assertEqual(
            shop.revoke_evidence(
                "thermal-declaration",
                expected_model_sha256=self.body["expected_model_sha256"],
                reason=self.body["reason"],
            ),
            receipt.json(),
        )
        self.assertEqual(shop.evidence_revocation("thermal-declaration"), receipt.json())

    def test_authentication_role_grant_path_and_revision_failures_do_not_revoke(self):
        self.assertEqual(self.client.put(self.path, json=self.body).status_code, 401)
        for token in (
            self.identity.token(subject="editor"),
            self.identity.token(subject="outsider"),
            self.identity.token(roles=("shop:read",)),
            self.identity.token(roles=("shop:operate",)),
        ):
            headers = {"Authorization": "Bearer " + token}
            self.assertEqual(
                self.client.put(self.path, json=self.body, headers=headers).status_code, 403
            )
        self.assertEqual(self.client.get(self.path).status_code, 401)
        wrong = self.client.put(
            self.path, json={**self.body, "evidence_id": "other"}, headers=self.headers
        )
        self.assertEqual(wrong.status_code, 422)
        stale = self.client.put(
            self.path, json={**self.body, "expected_model_sha256": "0" * 64}, headers=self.headers
        )
        self.assertEqual(stale.status_code, 409)
        injected = self.client.put(
            self.path,
            json={**self.body, "revoked_at": "2000-01-01T00:00:00Z"},
            headers=self.headers,
        )
        self.assertEqual(injected.status_code, 422)
        self.assertEqual(self.client.get(self.path, headers=self.headers).status_code, 404)
        self.assertEqual(
            self.operations.published.evaluation("thermal-example", request())["outcome"],
            "supported",
        )


if __name__ == "__main__":
    unittest.main()
