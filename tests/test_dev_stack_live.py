"""Opt-in checks against an already bootstrapped local stack; never real equipment."""

import base64
import json
import os
import secrets
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("CHIPLOAD_LIVE_TESTS") == "1", "requires ./scripts/dev-stack up and CHIPLOAD_LIVE_TESTS=1")
class LiveStackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config = json.loads(subprocess.check_output(
            ["docker", "compose", "--project-directory", str(ROOT), "config", "--format", "json"],
            text=True,
        ))
        cls.config = config
        cls.bao = "http://127.0.0.1:" + str(config["services"]["openbao"]["ports"][0]["published"])
        cls.keycloak_port = config["services"]["keycloak"]["ports"][0]["published"]
        cls.keycloak = f"http://localhost:{cls.keycloak_port}"
        cls.admin = json.loads((ROOT / ".local/dev-stack/openbao-init.json").read_text())["root_token"]
        cls.workload = json.loads((ROOT / ".local/dev-stack/runtime-approle.json").read_text())

    def request(self, base, path, *, method="GET", data=None, token=None, form=False, bao=False):
        headers = {}
        if token:
            headers["X-Vault-Token" if bao else "Authorization"] = token if bao else "Bearer " + token
        if form:
            body = urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode() if data is not None else None
            headers["Content-Type"] = "application/json"
        request = Request(base + path, method=method, data=body, headers=headers)
        with build_opener(ProxyHandler({})).open(request, timeout=15) as response:
            body = response.read()
            return json.loads(body) if body else None

    def test_keycloak_discovery_and_explicit_role_configuration(self):
        discovery = self.request(self.keycloak, "/realms/chipload/.well-known/openid-configuration")
        self.assertEqual(discovery["issuer"], self.keycloak + "/realms/chipload")
        self.assertIn("S256", discovery["code_challenge_methods_supported"])
        environment = self.config["services"]["keycloak"]["environment"]
        admin = self.request(self.keycloak, "/realms/master/protocol/openid-connect/token", method="POST", form=True, data={
            "grant_type": "password", "client_id": "admin-cli",
            "username": environment["KC_BOOTSTRAP_ADMIN_USERNAME"],
            "password": environment["KC_BOOTSTRAP_ADMIN_PASSWORD"],
        })["access_token"]
        clients = self.request(self.keycloak, "/admin/realms/chipload/clients", token=admin)
        resource = next(client for client in clients if client["clientId"] == "chipload")
        roles = self.request(self.keycloak, f"/admin/realms/chipload/clients/{resource['id']}/roles", token=admin)
        self.assertEqual({role["name"] for role in roles}, {"shop:read", "shop:operate", "shop:admin"})
        client = next(client for client in clients if client["clientId"] == "chipload-cli")
        self.assertFalse(client["directAccessGrantsEnabled"])
        self.assertTrue(client["publicClient"])
        self.assertEqual(client["attributes"]["pkce.code.challenge.method"], "S256")

        # Exercise the imported audience/role mappers with a disposable service identity.
        # Tokens are obtained directly from Keycloak and verified by the API boundary.
        client_id = "chipload-smoke-" + secrets.token_hex(8)
        client_secret = secrets.token_urlsafe(32)
        self.request(self.keycloak, "/admin/realms/chipload/clients", method="POST", token=admin, data={
            "clientId": client_id, "protocol": "openid-connect", "publicClient": False,
            "secret": client_secret, "serviceAccountsEnabled": True,
            "standardFlowEnabled": False, "directAccessGrantsEnabled": False,
            "protocolMappers": [
                {key: value for key, value in mapper.items() if key != "id"}
                for mapper in client["protocolMappers"]
            ] + [{
                "name": "chipload-resource-uri", "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "config": {"included.custom.audience": "http://localhost:8088/mcp",
                           "access.token.claim": "true", "id.token.claim": "false"},
            }],
        })
        created = self.request(self.keycloak, "/admin/realms/chipload/clients?" + urlencode({"clientId": client_id}), token=admin)[0]
        client_path = f"/admin/realms/chipload/clients/{created['id']}"
        self.addCleanup(self.request, self.keycloak, client_path, method="DELETE", token=admin)
        identity = self.request(self.keycloak, client_path + "/service-account-user", token=admin)
        read_role = next(role for role in roles if role["name"] == "shop:read")
        self.request(self.keycloak, f"/admin/realms/chipload/users/{identity['id']}/role-mappings/clients/{resource['id']}", method="POST", token=admin, data=[read_role])
        issued = self.request(self.keycloak, "/realms/chipload/protocol/openid-connect/token", method="POST", form=True, data={
            "grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret,
        })
        payload = issued["access_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
        self.assertIn("chipload", audiences)
        self.assertEqual(claims["iss"], discovery["issuer"])
        self.assertEqual(claims["resource_access"]["chipload"]["roles"], ["shop:read"])
        from fastapi.testclient import TestClient

        from chipload.access import ACTIONS, Grant, Principal
        from chipload.auth import ServerSettings, TokenVerifier
        from chipload.lifecycle import Repository, ShopService
        from chipload.operations import Operations
        from chipload.project import SourceFile, SourceProject
        from chipload.server import create_app

        with tempfile.TemporaryDirectory() as directory:
            settings = ServerSettings(
                mode="development", database=Path(directory) / "shop.db",
                resource_url="http://localhost:8088/mcp", issuer=discovery["issuer"],
                jwks_url=discovery["jwks_uri"],
                bindings=(Principal(subject=identity["id"], grants=(
                    Grant(shop_id="example", actions=("source:read", "source:write", "code:run")),
                )),),
            )
            service = ShopService(Repository(settings.database))
            operator = Principal(subject="test-operator", grants=(Grant(shop_id="example", actions=ACTIONS),))
            source = SourceProject(
                shop_id="example", entrypoint="shop:define_shop",
                files=(SourceFile(path="shop.py", content="def define_shop(): pass\n"),),
            )
            service.save_source(operator, source, expected_head=None)
            verifier = TokenVerifier(settings)
            try:
                with TestClient(
                    create_app(settings, operations=Operations(service), verifier=verifier),
                    base_url="http://localhost:8088",
                ) as api:
                    headers = {"Authorization": "Bearer " + issued["access_token"]}
                    response = api.get("/v1/shops/example/source", headers=headers)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(api.get("/v1/shops/another-shop/source", headers=headers).status_code, 403)
                    # Explicit binding cannot exceed this token's read-only role.
                    self.assertEqual(api.put("/v1/shops/example/source", headers=headers, json={
                        "project": source.model_dump(mode="json"), "expected_head": source.revision,
                    }).status_code, 403)
                    from program_support import wire

                    mcp = wire(api, "chipload://identity/current", {}, headers, method="resources/read")
                    self.assertEqual(mcp.status_code, 200, mcp.text)
                    self.assertEqual(json.loads(mcp.json()["result"]["contents"][0]["text"]), api.get("/v1/identity", headers=headers).json())
                    denied_code = wire(api, "run_python", {"shop_id": "example", "request_key": "read-only", "code": "result=1"}, headers)
                    self.assertEqual(denied_code.json()["result"]["structuredContent"]["status"], 403)
            finally:
                verifier.close()
        with self.assertRaises(HTTPError) as denied:
            self.request(self.keycloak, "/realms/chipload/protocol/openid-connect/token", method="POST", form=True, data={
                "grant_type": "password", "client_id": "chipload-cli", "username": "unused", "password": "unused",
            })
        self.assertEqual(denied.exception.code, 400)
        denied.exception.close()

    def test_openbao_workload_can_only_read_its_shop(self):
        suffix = "smoke-" + secrets.token_hex(8)
        paths = [f"shops/example/{suffix}", f"shops/another-shop/{suffix}"]
        for path in paths:
            self.request(self.bao, "/v1/chipload/data/" + path, method="POST", data={"data": {"test_value": suffix}}, token=self.admin, bao=True)
            self.addCleanup(self.request, self.bao, "/v1/chipload/metadata/" + path, method="DELETE", token=self.admin, bao=True)
        token = self.request(self.bao, "/v1/auth/approle/login", method="POST", data={
            "role_id": self.workload["role_id"], "secret_id": self.workload["secret_id"],
        }, bao=True)["auth"]["client_token"]
        self.addCleanup(self.request, self.bao, "/v1/auth/token/revoke", method="POST", data={"token": token}, token=self.admin, bao=True)
        renewed = self.request(self.bao, "/v1/auth/token/renew-self", method="POST", data={}, token=token, bao=True)
        self.assertGreater(renewed["auth"]["lease_duration"], 0)
        value = self.request(self.bao, "/v1/chipload/data/" + paths[0], token=token, bao=True)
        self.assertEqual(value["data"]["data"]["test_value"], suffix)
        for path, method, data in (
            ("/v1/chipload/data/" + paths[0], "POST", {"data": {"test_value": "changed"}}),
            ("/v1/chipload/data/" + paths[1], "GET", None),
            ("/v1/sys/mounts", "GET", None),
        ):
            with self.subTest(path=path, method=method), self.assertRaises(HTTPError) as denied:
                self.request(self.bao, path, method=method, data=data, token=token, bao=True)
            self.assertEqual(denied.exception.code, 403)
            denied.exception.close()


if __name__ == "__main__":
    unittest.main()
