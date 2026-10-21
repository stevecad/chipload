"""Hostile HTTPS responses and actual disposable network-process deadlines."""

import os
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tls_support import stack

from chipload.client import ServiceError, TransportError
from chipload.federation_wire import MAX_BODY_BYTES, EgressPolicy, request


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.server.paths.append(self.path)
        self.server.hosts.append(self.headers["Host"])
        body = self.server.body
        if self.server.echo:
            body = ('{"value":"' + self.headers["Authorization"] + '"}').encode()
        time.sleep(self.server.delay)
        try:
            self.send_response(self.server.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if self.server.encoding:
                self.send_header("Content-Encoding", self.server.encoding)
            self.send_header("Location", self.server.origin + "/redirect-target")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass


class WireTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        stack.local_certificates(self.root / "tls", {"api": "localhost", "identity": "localhost"})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.origin = f"https://localhost:{self.server.server_port}"
        self.server.paths = []
        self.server.hosts = []
        self.server.status, self.server.delay = 200, 0
        self.server.body, self.server.encoding, self.server.echo = b'{"ok":true}', None, False
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.root / "tls/certificates/api.crt", self.root / "tls/certificates/api.key")
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.policy = EgressPolicy(private_origins=(self.server.origin,), ca_file=self.root / "tls/identity-ca.pem", seconds=2)

    def test_redirect_is_returned_as_failure_without_contacting_its_target(self):
        self.server.status = 302
        with self.assertRaises(ServiceError) as redirected:
            request(self.server.origin, "GET", "/source", policy=self.policy)
        self.assertEqual(redirected.exception.status, 302)
        self.assertEqual(self.server.paths, ["/source"])

    def test_private_dial_route_preserves_logical_host_and_tls_name(self):
        logical = "https://localhost"
        policy = EgressPolicy(private_origins=(logical,), ca_file=self.policy.ca_file, seconds=self.policy.seconds,
            routes=({"origin": logical, "port": self.server.server_port},))
        self.assertEqual(request(logical, "GET", "/source", policy=policy), {"ok": True})
        self.assertEqual(self.server.hosts, ["localhost"])
        with self.assertRaises(ValueError):
            EgressPolicy(routes=({"origin": logical, "port": self.server.server_port},))
        with self.assertRaises(ValueError):
            EgressPolicy(private_origins=(logical,), routes=(
                {"origin": logical, "port": 443}, {"origin": logical, "port": 8443}))

    def test_duplicate_keys_compression_oversized_output_and_credential_echo_are_rejected(self):
        cases = ((b'{"a":1,"a":2}', None, False),
                 (b'{"ok":true}', "gzip", False),
                 (b"x" * (MAX_BODY_BYTES + 1), None, False),
                 (b"", None, True))
        for body, encoding, echo in cases:
            self.server.body, self.server.encoding, self.server.echo = body, encoding, echo
            with self.subTest(size=len(body), encoding=encoding, echo=echo), self.assertRaises(TransportError):
                request(self.server.origin, "GET", "/source", policy=self.policy, token="fixture-only-token")
        self.assertEqual(len(self.server.paths), 4)

    def test_slow_tls_reply_has_a_wall_deadline_and_is_not_retried(self):
        self.server.delay = 8
        started = time.monotonic()
        with self.assertRaises(TransportError):
            request(self.server.origin, "GET", "/source", policy=self.policy.model_copy(update={"seconds": 1}))
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(self.server.paths, ["/source"])

    def test_escaped_credentials_in_response_values_keys_and_errors_are_rejected(self):
        token = "fixture-only-token"
        escaped = "".join("\\u%04x" % ord(character) for character in token)
        for status, body in ((200, '{"value":"' + escaped + '"}'),
                             (200, '{"' + escaped + '":true}'),
                             (403, '{"error":{"message":"' + escaped + '"}}')):
            self.server.status, self.server.body = status, body.encode()
            with self.subTest(status=status, body=body), self.assertRaises(TransportError):
                request(self.server.origin, "GET", "/source", policy=self.policy, token=token)

    def test_correct_ca_with_the_wrong_tls_hostname_is_rejected(self):
        origin = self.server.origin.replace("localhost", "127.0.0.1")
        policy = self.policy.model_copy(update={"private_origins": (origin,)})
        with self.assertRaises(TransportError):
            request(origin, "GET", "/source", policy=policy)
        self.assertEqual(self.server.paths, [])

    def test_actual_stalled_dns_worker_is_killed_by_the_parent_deadline(self):
        original = subprocess.run
        receipt = self.root / "pid"
        child = (
            "import os,socket,time; from pathlib import Path; "
            f"Path({str(receipt)!r}).write_text(str(os.getpid())); "
            "socket.getaddrinfo=lambda *a,**k: time.sleep(30); "
            "from chipload.federation_wire import main; main()"
        )
        def stalled(command, **kwargs):
            return original([command[0], "-c", child], **kwargs)
        started = time.monotonic()
        with patch("subprocess.run", side_effect=stalled), self.assertRaises(TransportError):
            request("https://public.example", "GET", "/source", policy=EgressPolicy(seconds=1))
        self.assertLess(time.monotonic() - started, 4)
        self.assertTrue(receipt.exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(int(receipt.read_text()), 0)
