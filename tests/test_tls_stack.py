"""TLS setup preserves identity state and verifies certificates and hostnames."""

import contextlib
import io
import json
import ssl
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tls_support import stack


class TLSStackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.values = {
            "version": 1,
            "profile": "local",
            "name": "tls-test",
            "api_host": "chipload.localhost",
            "identity_host": "identity.localhost",
            "port": 8443,
            "admin_port": 8444,
            "subnet": "10.240.1.0/28",
            "shop_id": "example",
        }

    def test_deployment_configuration_rejects_ambiguous_hosts_profiles_ports_and_networks(self):
        self.assertEqual(stack.configuration(self.values), self.values)
        for changes in (
            {"api_host": "host:443"},
            {"api_host": "host\nrespond 200"},
            {"api_host": "host..localhost"},
            {"identity_host": "chipload.localhost"},
            {"profile": "certificates"},
            {"port": True},
            {"port": 0},
            {"port": 8444},
            {"subnet": "0.0.0.0/0"},
            {"subnet": "127.0.0.0/28"},
            {"subnet": "::/128"},
            {"name": "../../another-project"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                stack.configuration({**self.values, **changes})
        supplied = {
            **self.values,
            "profile": "certificates",
            "api_host": "shop.example.com",
            "identity_host": "identity.example.com",
            "port": 443,
        }
        self.assertEqual(stack.configuration(supplied), supplied)
        self.assertEqual(stack.origin("shop.example.com", 443), "https://shop.example.com")

    def test_subnet_selection_handles_non_ip_networks_and_rejects_overlap(self):
        output = [
            "one two",
            json.dumps(
                [{"IPAM": {"Config": None}}, {"IPAM": {"Config": [{"Subnet": "10.240.1.0/24"}]}}]
            ),
        ]
        with patch.object(stack.subprocess, "check_output", side_effect=output):
            with self.assertRaises(ValueError):
                stack.choose_subnet("10.240.1.0/28")

    def test_client_listener_is_explicit_and_cannot_expose_the_local_certificate_profile(self):
        self.assertEqual(stack.connect_address(self.values), "127.0.0.1")
        for address in ("0.0.0.0", "192.0.2.10", "::", "localhost", "127.0.0.1:443", None):
            with self.subTest(address=address), self.assertRaises(ValueError):
                stack.configuration({**self.values, "listen_address": address})
        supplied = {
            **self.values,
            "profile": "certificates",
            "api_host": "shop.example.test",
            "identity_host": "identity.example.test",
        }
        for address, connection in (("0.0.0.0", "127.0.0.1"), ("192.0.2.10", "192.0.2.10")):
            configured = stack.configuration({**supplied, "listen_address": address})
            self.assertEqual(stack.listen_address(configured), address)
            self.assertEqual(stack.connect_address(configured), connection)
            self.assertEqual(stack.connect_address(configured, admin=True), "127.0.0.1")
        for address in ("239.0.0.1", "169.254.1.2", "255.255.255.255", "::1"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                stack.configuration({**supplied, "listen_address": address})

    def test_public_listener_requires_engine_with_current_loopback_port_isolation(self):
        with patch.object(stack.subprocess, "check_output") as query:
            stack.check_public_engine(self.values)
            query.assert_not_called()
        public = {**self.values, "profile": "certificates", "listen_address": "0.0.0.0"}
        for version in ("27.5.1", "unknown", "true"):
            with patch.object(stack.subprocess, "check_output", return_value=version):
                with self.assertRaisesRegex(ValueError, "28 or later"):
                    stack.check_public_engine(public)
        with patch.object(stack.subprocess, "check_output", return_value="29.8.1\n"):
            stack.check_public_engine(public)

    def test_initialization_keeps_credentials_private_and_never_replaces_existing_state(self):
        args = SimpleNamespace(
            **{
                **self.values,
                "state": self.root / "state",
                "local": True,
                "shop": "example",
                "certificates": None,
                "ca_bundle": None,
            }
        )
        with (
            patch.object(stack.subprocess, "check_output", return_value=""),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            stack.initialize(args)
            before = {
                str(path.relative_to(args.state)): path.read_bytes()
                for path in args.state.rglob("*")
                if path.is_file()
            }
            with self.assertRaises(ValueError):
                stack.initialize(args)
            after = {
                str(path.relative_to(args.state)): path.read_bytes()
                for path in args.state.rglob("*")
                if path.is_file()
            }
        self.assertTrue(before == after)
        for path in args.state.rglob("*"):
            expected = 0o700 if path.is_dir() else 0o444 if path.name == "db-password" else 0o600
            self.assertEqual(path.stat().st_mode & 0o777, expected, path.name)
        runtime = json.loads((args.state / "runtime.json").read_bytes())
        client = json.loads((args.state / "client.json").read_bytes())
        realm = json.loads((args.state / "realm.json").read_bytes())
        self.assertEqual(runtime["bindings"][0]["subject"], client["subject"])
        self.assertEqual(realm["users"][0]["id"], client["subject"])
        self.assertEqual(runtime["bindings"][0]["grants"][0]["shop_id"], "example")
        self.assertIn("network:query", runtime["bindings"][0]["grants"][0]["actions"])
        self.assertNotIn("network:dispatch", runtime["bindings"][0]["grants"][0]["actions"])
        self.assertEqual(realm["sslRequired"], "all")
        self.assertFalse(
            next(c for c in realm["clients"] if c["clientId"] == "chipload-agent")[
                "directAccessGrantsEnabled"
            ]
        )
        self.assertTrue((args.state / "recovery/local-ca-key.pem").is_file())
        self.assertFalse((args.state / "certificates/local-ca-key.pem").exists())
        stack.check_material(args.state)
        key = args.state / "certificates/api.key"
        key.chmod(0o644)
        with self.assertRaises(ValueError):
            stack.check_material(args.state)
        key.chmod(0o600)
        password = args.state / "secrets/db-password"
        password.chmod(0o644)
        with self.assertRaises(ValueError):
            stack.check_material(args.state)
        password.chmod(0o444)
        original = key.read_bytes()
        key.write_bytes((args.state / "certificates/identity.key").read_bytes())
        with self.assertRaises(ssl.SSLError):
            stack.check_material(args.state)
        key.write_bytes(original)
        key.unlink()
        key.symlink_to(args.state / "certificates/identity.key")
        with self.assertRaises(ValueError):
            stack.check_material(args.state)

    def test_gateway_and_identity_images_must_match_their_roles_and_checkout(self):
        import hashlib

        images = {"gateway": "sha256:gateway", "keycloak": "sha256:keycloak"}
        labels = []
        for role in images:
            paths = [stack.ROOT / f"deploy/tls/{role}.Dockerfile", stack.ROOT / "LICENSE"]
            if role == "keycloak":
                paths.append(stack.ROOT / "deploy/tls/keycloak-entrypoint.sh")
            else:
                paths.extend(stack.ROOT / "deploy/tls" / f"gateway.go.{suffix}" for suffix in ("mod", "sum"))
            labels.append(
                json.dumps(
                    {
                        "org.chipload.tls-role": role,
                        "org.chipload.tls-sha256": hashlib.sha256(
                            b"".join(p.read_bytes() for p in paths)
                        ).hexdigest(),
                    }
                )
            )
        with patch.object(stack.subprocess, "check_output", side_effect=labels):
            stack.check_tls_images(images)
        for invalid in (
            "null",
            labels[1],
            json.dumps({"org.chipload.tls-role": "gateway", "org.chipload.tls-sha256": "old"}),
        ):
            with patch.object(stack.subprocess, "check_output", return_value=invalid):
                with self.assertRaises(ValueError):
                    stack.check_tls_images(images)

    def test_material_rejects_matching_key_and_certificate_for_the_wrong_hostname(self):
        args = SimpleNamespace(
            **self.values,
            state=self.root / "state",
            local=True,
            shop="example",
            certificates=None,
            ca_bundle=None,
        )
        with (
            patch.object(stack.subprocess, "check_output", return_value=""),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            stack.initialize(args)
        for suffix in ("crt", "key"):
            (args.state / f"certificates/api.{suffix}").write_bytes(
                (args.state / f"certificates/identity.{suffix}").read_bytes()
            )
        with self.assertRaises((ValueError, ssl.SSLError)):
            stack.check_material(args.state)

    def test_existing_deployment_volume_prevents_creating_replacement_credentials(self):
        args = SimpleNamespace(state=self.root / "state", name="existing")
        with patch.object(stack.subprocess, "check_output", return_value="existing-volume\n"):
            with self.assertRaises(ValueError):
                stack.initialize(args)
        self.assertFalse(args.state.exists())

    def test_loopback_probe_checks_chain_and_hostname_without_host_dns_changes(self):
        state = self.root / "certificates"
        stack.local_certificates(state, {"api": "shop.example.test"})

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(state / "certificates/api.crt", state / "certificates/api.key")
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            ca = state / "identity-ca.pem"
            port = server.server_address[1]
            self.assertEqual(stack.probe("shop.example.test", port, "/healthz", ca), {"ok": True})
            with self.assertRaises(ssl.SSLCertVerificationError):
                stack.probe("wrong.example.test", port, "/healthz", ca)
            other = self.root / "other"
            stack.local_certificates(other, {"api": "shop.example.test"})
            with self.assertRaises(ssl.SSLCertVerificationError):
                stack.probe("shop.example.test", port, "/healthz", other / "identity-ca.pem")
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
