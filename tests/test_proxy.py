"""Only the configured gateway can supply a single client address for admission."""

import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError
from server_support import IdentityFixture

from chipload.auth import ServerSettings
from chipload.quotas import QuotaPolicy
from chipload.server import create_app


class ProxyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.identity = IdentityFixture(temporary.name)
        self.addCleanup(self.identity.client.close)
        self.settings = self.identity.settings.model_copy(
            update={"trusted_proxy_ips": ("192.0.2.10",)}
        )
        self.operations = self.identity.operations()
        self.operations.limiter.clock = lambda: 1_792_454_400_000
        state = self.operations.service.repository.read_quotas()
        state["policy"]["requests"]["peer"]["burst"] = 1
        self.operations.service.repository.set_quotas(
            QuotaPolicy.model_validate(state["policy"]), expected_generation=state["generation"]
        )

    def client(self, address="192.0.2.10"):
        client = TestClient(
            create_app(self.settings, operations=self.operations, verifier=self.identity.verifier),
            base_url=self.settings.base_url,
            client=(address, 50000),
        )
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def test_gateway_clients_have_distinct_budgets_shared_across_rest_and_mcp(self):
        client = self.client()
        headers = {"X-Forwarded-For": "198.51.100.1"}
        self.assertEqual(client.get("/healthz", headers=headers).status_code, 200)
        denied = client.post("/mcp", headers=headers, json={})
        self.assertEqual(denied.status_code, 429)
        self.assertEqual(denied.json()["error"]["data"]["error"]["limit"], "peer")
        self.assertEqual(
            client.get("/healthz", headers={"X-Forwarded-For": "198.51.100.2"}).status_code, 200
        )

    def test_other_peers_cannot_assert_client_addresses_even_when_proxy_trust_is_enabled(self):
        client = self.client("192.0.2.11")
        self.assertEqual(
            client.get("/healthz", headers={"X-Forwarded-For": "198.51.100.1"}).status_code, 200
        )
        self.assertEqual(
            client.get("/healthz", headers={"X-Forwarded-For": "198.51.100.2"}).status_code, 429
        )

    def test_gateway_requires_one_unambiguous_ip_before_verifying_tokens(self):
        client = self.client()
        with patch.object(
            self.identity.verifier, "verify", side_effect=AssertionError("verified token")
        ):
            for value in (
                None,
                "",
                "unknown",
                "198.51.100.1, 198.51.100.2",
                " 198.51.100.1",
                "[::1]:123",
                "fe80::1%eth0",
                "0.0.0.0",
                "ff02::1",
                "::ffff:0.0.0.0",
                "::ffff:224.0.0.1",
            ):
                headers = {"Authorization": "Bearer fixture"}
                if value is not None:
                    headers["X-Forwarded-For"] = value
                with self.subTest(value=value):
                    response = client.get("/healthz", headers=headers)
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertEqual(response.json()["error"]["code"], "invalid_proxy_address")
            duplicate = client.get(
                "/healthz",
                headers=[("X-Forwarded-For", "198.51.100.1"), ("X-Forwarded-For", "198.51.100.2")],
            )
            self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(
            client.get("/healthz", headers={"X-Forwarded-For": "198.51.100.1"}).status_code, 200
        )

    def test_forwarded_ipv6_addresses_keep_the_existing_subnet_budget(self):
        client = self.client()
        self.assertEqual(
            client.get("/healthz", headers={"X-Forwarded-For": "2001:db8::1"}).status_code, 200
        )
        self.assertEqual(
            client.get("/healthz", headers={"X-Forwarded-For": "2001:db8::2"}).status_code, 429
        )
        self.assertEqual(
            client.get("/healthz", headers={"X-Forwarded-For": "2001:db8:0:1::1"}).status_code, 200
        )

    def test_configuration_requires_exact_proxy_addresses_and_an_absolute_ca_path(self):
        values = self.identity.settings.model_dump()
        for proxy in (
            "*",
            "192.0.2.0/24",
            "0.0.0.0",
            "::",
            "ff02::1",
            "fe80::1%eth0",
            "::ffff:0.0.0.0",
            "::ffff:224.0.0.1",
        ):
            with self.subTest(proxy=proxy), self.assertRaises(ValidationError):
                ServerSettings.model_validate({**values, "trusted_proxy_ips": [proxy]})
        with self.assertRaises(ValidationError):
            ServerSettings.model_validate(
                {**values, "trusted_proxy_ips": ["192.0.2.10", "::ffff:192.0.2.10"]}
            )
        with self.assertRaises(ValidationError):
            ServerSettings.model_validate({**values, "jwks_ca_file": "relative.pem"})
        self.assertEqual(
            ServerSettings.model_validate(
                {**values, "trusted_proxy_ips": ["::ffff:192.0.2.10"]}
            ).trusted_proxy_ips,
            ("192.0.2.10",),
        )


if __name__ == "__main__":
    unittest.main()
