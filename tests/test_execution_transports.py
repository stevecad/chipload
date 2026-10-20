"""REST and MCP share operation permissions, durable identities, and safe projections."""

import tempfile
import unittest
from pathlib import Path

from execution_support import ExecutionFixture
from fastapi.testclient import TestClient
from program_support import call as program_call
from program_support import processor
from server_support import IdentityFixture

from chipload.access import ACTIONS, Grant, Principal
from chipload.auth import TokenVerifier
from chipload.operations import Operations
from chipload.server import create_app


class ExecutionTransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.f = ExecutionFixture(Path(temporary.name) / "shop.db")
        self.identity = IdentityFixture(temporary.name)
        self.addCleanup(self.identity.client.close)
        limited = Principal(
            subject="limited",
            grants=(Grant(shop_id=self.f.shop, actions=ACTIONS, operations=("inspect-sample",)),),
        )
        settings = self.identity.settings.model_copy(
            update={
                "database": self.f.repository.path,
                "bindings": (self.f.owner, limited),
            }
        )
        operations = Operations(self.f.service)
        operations.executions = self.f.execution
        self.programs = processor(operations, settings)
        app = create_app(
            settings,
            operations=operations,
            verifier=TokenVerifier(settings, client=self.identity.client),
        )
        self.client = TestClient(app, base_url=settings.base_url)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.headers = {"Authorization": "Bearer " + self.identity.token(roles=("shop:operate",))}
        self.path = "/v1/shops/callable-example/operations/make-sample/executions"

    def mcp(self, name, request, headers=None):
        response = program_call(self.client, name, request, headers if headers is not None else self.headers, worker=self.programs)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["result"]

    def test_retry_inspect_cancel_and_completion_have_transport_parity(self):
        body = self.f.request().model_dump(mode="json")
        rest = self.client.post(self.path, json=body, headers=self.headers)
        self.assertEqual(rest.status_code, 202, rest.text)
        repeated = self.mcp("operation_submit", body)
        self.assertFalse(repeated.get("isError"), repeated)
        self.assertEqual(rest.json(), repeated["structuredContent"])
        identifier = rest.json()["id"]
        read = {"shop_id": self.f.shop, "operation_id": "make-sample", "execution_id": identifier}
        self.assertTrue(self.f.runner.run_next())
        completed = self.client.get(self.path + "/" + identifier, headers=self.headers)
        self.assertEqual(completed.json()["state"], "succeeded")
        self.assertEqual(
            completed.json(), self.mcp("operation_execution_read", read)["structuredContent"]
        )
        inspection = self.client.get(
            "/v1/shops/callable-example/private/operations/make-sample", headers=self.headers
        )
        self.assertEqual(
            inspection.json(),
            self.mcp(
                "operation_inspect",
                {
                    "shop_id": self.f.shop,
                    "operation_id": "make-sample",
                },
            )["structuredContent"],
        )
        self.assertTrue(inspection.json()["enabled"])
        body["request_key"] = "cancel"
        job = self.mcp("operation_submit", body)["structuredContent"]
        read["execution_id"] = job["id"]
        cancelled = self.mcp("operation_execution_cancel", read)["structuredContent"]
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(
            cancelled,
            self.client.post(self.path + "/" + job["id"] + "/cancel", headers=self.headers).json(),
        )

    def test_operation_grants_and_role_ceiling_are_enforced_on_both_transports(self):
        body = self.f.request().model_dump(mode="json")
        self.assertEqual(self.client.post(self.path, json=body).status_code, 401)
        for token in (
            self.identity.token(roles=("shop:read",)),
            self.identity.token("limited"),
            self.identity.token("unbound"),
        ):
            headers = {"Authorization": "Bearer " + token}
            rest = self.client.post(self.path, json=body, headers=headers)
            self.assertEqual(rest.status_code, 403)
            mcp = self.mcp("operation_submit", body, headers)
            self.assertTrue(mcp["isError"])
            self.assertEqual(mcp["structuredContent"], {"status": 403, **rest.json()})
        other = {**body, "shop_id": "other-shop"}
        self.assertEqual(
            self.client.post(
                self.path.replace("callable-example", "other-shop"),
                json=other,
                headers=self.headers,
            ).status_code,
            403,
        )
        denied = self.client.get("/v1/shops/callable-example/source", headers=self.headers)
        self.assertEqual(denied.status_code, 403)

    def test_public_operations_reveal_contracts_without_adapter_authority(self):
        response = self.client.get("/v1/shops/callable-example/operations/make-sample")
        self.assertEqual(response.status_code, 200)
        for private in (
            "adapter",
            "command",
            "checks",
            "model_sha256",
            "resources",
            "machine-link",
        ):
            self.assertNotIn('"' + private + '"', response.text)
        public = self.mcp(
            "shop_inspect",
            {"shop_id": self.f.shop, "collection": "operations", "item_id": "make-sample"},
            {},
        )
        self.assertFalse(public.get("isError"), public)
        self.assertEqual(public["structuredContent"], response.json())
        schema = self.client.get("/openapi.json").json()
        path = "/v1/shops/{shop_id}/operations/{operation_id}/executions"
        self.assertTrue(schema["paths"][path]["post"]["security"])

    def test_business_denial_and_conflicting_retry_agree(self):
        body = self.f.request().model_dump(mode="json")
        self.client.post(self.path, json=body, headers=self.headers)
        body["inputs"][0]["value"]["value"] = "200"
        for key in ("first", "different"):
            body["request_key"] = key
            rest = self.client.post(self.path, json=body, headers=self.headers)
            self.assertEqual(rest.status_code, 409, rest.text)
            mcp = self.mcp("operation_submit", body)
            self.assertTrue(mcp["isError"])
            self.assertEqual(mcp["structuredContent"], {"status": 409, **rest.json()})
