"""Operator credentials use verified local TLS without redirect or proxy forwarding."""

import ssl
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from credentials_support import credentials as script


class CredentialHTTPTests(unittest.TestCase):
    def test_tls_names_ca_trust_response_bounds_and_redirects(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            script.stack.local_certificates(state, {"identity": "identity.localhost"})
            seen = []

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    seen.append(self.path)
                    self.send_response(302 if self.path == "/redirect" else 200)
                    self.send_header("Location", "https://elsewhere.invalid/credential")
                    self.end_headers()
                    self.wfile.write(b"x" * (1024 * 1024 + 1) if self.path == "/large" else b"{}")

                def log_message(self, *args):
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(
                state / "certificates/identity.crt", state / "certificates/identity.key"
            )
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            values = {"identity_host": "identity.localhost", "port": server.server_port}
            try:
                with patch.dict(
                    "os.environ",
                    {"HTTPS_PROXY": "http://127.0.0.1:1", "ALL_PROXY": "http://127.0.0.1:1"},
                ):
                    self.assertEqual(
                        script.request(state, values, "/ok", token="fixture"), (200, {})
                    )
                self.assertEqual(
                    script.request(state, values, "/redirect", token="fixture"), (302, {})
                )
                self.assertEqual(seen, ["/ok", "/redirect"])
                with self.assertRaisesRegex(ValueError, "exceeded its limit"):
                    script.request(state, values, "/large")
                with self.assertRaises(ssl.SSLCertVerificationError):
                    script.request(
                        state,
                        {**values, "identity_host": "other.localhost"},
                        "/wrong-name",
                        token="fixture",
                    )
                other = state / "other"
                script.stack.local_certificates(other, {"identity": "identity.localhost"})
                with self.assertRaises(ssl.SSLCertVerificationError):
                    script.request(other, values, "/wrong-ca", token="fixture")
                self.assertNotIn("/wrong-name", seen)
                self.assertNotIn("/wrong-ca", seen)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
