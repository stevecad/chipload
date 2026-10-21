"""Actual TLS workload secret exchange, token verification and scoped failures."""

import json
import ssl
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
from server_support import IdentityFixture
from tls_support import stack

from chipload.client import TransportError
from chipload.connector_auth import (
    CredentialUnavailable,
    KeyTransport,
    WorkloadIdentity,
    WorkloadToken,
)
from chipload.federation_wire import EgressPolicy, request


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, value=None):
        body = json.dumps(value).encode() if value is not None else b""
        self.send_response(status)
        if value is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        server = self.server
        server.paths.append(self.path)
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path == "/v1/auth/approle/login":
            if json.loads(raw) != {"role_id": "fixture-role", "secret_id": "fixture-secret"}:
                return self.reply(403, {"errors": ["denied"]})
            return self.reply(200, {"auth": {"client_token": "fixture-bao-token", "policies": server.policies,
                                             "lease_duration": server.lease}})
        if self.path == "/v1/auth/token/revoke-self":
            if self.headers.get("X-Vault-Token") != "fixture-bao-token" or self.headers.get("Authorization"):
                return self.reply(403, {"errors": ["denied"]})
            return self.reply(204)
        if self.path == "/realms/workload/protocol/openid-connect/token":
            if (self.headers.get_content_type() != "application/x-www-form-urlencoded"
                    or parse_qs(raw.decode()) != {"grant_type": ["client_credentials"],
                        "client_id": ["connector-cloud"], "client_secret": [server.secret]}):
                return self.reply(401, {"error": "invalid_client"})
            server.oauth_calls += 1
            if server.echo:
                return self.reply(200, {"access_token": server.secret})
            token = server.identity.token("cloud-owner", changes={"azp": "connector-cloud", **server.changes})
            return self.reply(200, {"access_token": token, "expires_in": server.expires, "token_type": "Bearer"})
        self.reply(404, {})

    def do_GET(self):
        server = self.server
        server.paths.append(self.path)
        if self.path == "/certs":
            if self.headers.get("Authorization") or self.headers.get("X-Vault-Token"):
                return self.reply(403, {})
            return self.reply(200, {"keys": server.identity.keys})
        if self.path == "/v1/connector/data/cloud/client":
            if self.headers.get("X-Vault-Token") != "fixture-bao-token" or self.headers.get("Authorization"):
                return self.reply(403, {"errors": ["denied"]})
            if not server.available:
                return self.reply(403, {"errors": ["denied"]})
            return self.reply(200, {"data": {"data": {"client-secret": server.secret},
                "metadata": {"version": 1, "destroyed": server.destroyed, "deletion_time": ""}}})
        self.reply(403, {"errors": ["denied"]})


class ConnectorAuthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        stack.local_certificates(self.root / "tls", {"api": "localhost", "identity": "localhost"})
        self.credentials = self.root / "credentials"
        self.credentials.mkdir(mode=0o700)
        for name, value in (("role-id", "fixture-role"), ("secret-id", "fixture-secret")):
            path = self.credentials / name
            path.touch(mode=0o600)
            path.write_text(value)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        origin = f"https://localhost:{self.server.server_port}"
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.root / "tls/certificates/api.crt", self.root / "tls/certificates/api.key")
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        identity = IdentityFixture(self.root)
        self.addCleanup(identity.client.close)
        identity.settings = identity.settings.model_copy(update={"mode": "production", "bindings": (),
            "issuer": origin + "/realms/workload", "jwks_url": origin + "/certs",
            "resource_url": "https://cloud.example/mcp"})
        self.server.identity = identity
        self.server.paths, self.server.policies = [], ["connector-cloud"]
        self.server.lease, self.server.secret = 60, "fixture-client-secret"
        self.server.available, self.server.destroyed, self.server.echo = True, False, False
        self.server.oauth_calls, self.server.expires, self.server.changes = 0, 300, {}
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.config = WorkloadIdentity(client_id="connector-cloud", subject="cloud-owner", trust=identity.settings,
            policy=EgressPolicy(private_origins=(origin,), ca_file=self.root / "tls/identity-ca.pem", seconds=3),
            secrets={"origin": origin, "kv_mount": "connector", "secret_path": "cloud/client",
                     "field": "client-secret", "policy": "connector-cloud"})
        self.provider = WorkloadToken(self.config, self.credentials)
        self.addCleanup(self.provider.close)

    def test_scoped_secret_exchange_verified_token_cache_and_rotation(self):
        first = self.provider()
        self.assertEqual(self.server.oauth_calls, 1)
        self.assertEqual(self.provider(), first)
        self.assertEqual(self.server.oauth_calls, 1)
        self.assertEqual(self.server.paths, ["/v1/auth/approle/login", "/v1/connector/data/cloud/client",
            "/v1/auth/token/revoke-self", "/realms/workload/protocol/openid-connect/token", "/certs"])
        self.server.secret = "fixture-rotated-client-secret"
        self.provider.invalidate()
        self.provider()
        self.assertEqual(self.server.oauth_calls, 2)
        self.assertEqual(self.server.paths.count("/v1/auth/token/revoke-self"), 2)

    def test_wrong_token_audience_issuer_subject_client_and_expiry_are_refused(self):
        for changes in ({"aud": "https://other.example/mcp"}, {"iss": "https://other.example/realms/workload"},
                        {"sub": "other-owner"}, {"azp": "other-client"}, {"exp": 1}):
            self.server.changes = changes
            self.provider.invalidate()
            with self.subTest(changes=changes), self.assertRaises(CredentialUnavailable) as error:
                self.provider()
            self.assertEqual(str(error.exception), "Connector credential is unavailable")
        self.assertEqual(self.server.oauth_calls, 5)

    def test_overbroad_or_long_lived_bao_tokens_are_revoked_without_reading_the_secret(self):
        for policies, lease in ((["connector-cloud", "default"], 60), (["connector-cloud"], 600)):
            self.server.policies, self.server.lease = policies, lease
            with self.subTest(policies=policies, lease=lease), self.assertRaises(CredentialUnavailable):
                self.provider()
            self.assertEqual(self.server.paths[-1], "/v1/auth/token/revoke-self")
        self.assertNotIn("/v1/connector/data/cloud/client", self.server.paths)
        self.assertEqual(self.server.oauth_calls, 0)

    def test_denied_destroyed_and_echoed_secrets_fail_without_cached_fallback(self):
        self.provider()
        for available, destroyed, echo in ((False, False, False), (True, True, False), (True, False, True)):
            self.server.available, self.server.destroyed, self.server.echo = available, destroyed, echo
            self.provider.invalidate()
            with self.subTest(available=available, destroyed=destroyed, echo=echo), self.assertRaises(CredentialUnavailable):
                self.provider()
            self.assertIsNone(self.provider._token)
        self.assertEqual(self.server.paths.count("/v1/auth/token/revoke-self"), 4)

    def test_private_files_and_tls_destination_policy_are_enforced(self):
        (self.credentials / "secret-id").chmod(0o644)
        with self.assertRaises(CredentialUnavailable):
            self.provider()
        self.assertEqual(self.server.paths, [])
        (self.credentials / "secret-id").chmod(0o600)
        self.provider.config = self.config.model_copy(update={"policy": EgressPolicy(seconds=1)})
        with self.assertRaises(CredentialUnavailable):
            self.provider()
        self.assertEqual(self.server.paths, [])
        self.credentials.chmod(0o755)
        with self.assertRaises(ValueError):
            WorkloadToken(self.config, self.credentials)

    def test_form_encoding_cannot_expand_past_the_outgoing_byte_limit(self):
        with self.assertRaises(TransportError):
            request(self.config.secrets.origin, "POST", "/realms/workload/protocol/openid-connect/token",
                    policy=self.config.policy, encoding="form", body={"large": "%" * 1_100_000})
        self.assertEqual(self.server.paths, [])


class KeyTransportTests(unittest.TestCase):
    def test_explicit_default_port_is_normalized_before_exact_jwks_comparison(self):
        transport = KeyTransport("https://identity.example:443/certs", EgressPolicy())
        transport.deadline = time.monotonic() + 30
        with patch("chipload.connector_auth.request", return_value={"keys": []}) as request:
            reply = transport.handle_request(httpx.Request("GET", "https://identity.example/certs"))
            self.assertEqual(reply.json(), {"keys": []})
            self.assertEqual(request.call_args.args, ("https://identity.example", "GET", "/certs"))
        for method, url in (("POST", "https://identity.example/certs"), ("GET", "https://other.example/certs"),
                            ("GET", "https://identity.example/other")):
            with self.subTest(method=method, url=url), self.assertRaises(httpx.UnsupportedProtocol):
                transport.handle_request(httpx.Request(method, url))
