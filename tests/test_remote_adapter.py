"""Real TLS and process bounds; fixtures provide remote credentials and receipts."""

import json
import ssl
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from receiver_support import Receiver
from tls_support import stack

from chipload.artifact import digest
from chipload.execution_model import AdapterResult, ExecutionRequest
from chipload.executor import ExecutionContext
from chipload.model import Fact
from chipload.remote_adapter import HttpAdapter, HttpAdapterConfig, receiver_token
from examples.service_shop import define_shop


class BaoHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        self.server.paths.append(self.path)
        if self.path == "/v1/auth/approle/login":
            self.rfile.read(int(self.headers["Content-Length"]))
            value = {
                "auth": {
                    "client_token": "fixture-openbao-token",
                    "policies": [self.server.policy],
                    "lease_duration": 60,
                }
            }
            data = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(204)
            self.end_headers()

    def do_GET(self):
        self.server.paths.append(self.path)
        data = json.dumps(
            {
                "data": {
                    "data": {"token": self.server.token},
                    "metadata": {"version": 1, "destroyed": False, "deletion_time": ""},
                }
            }
        ).encode()
        self.send_response(200 if self.server.available else 503)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class RemoteAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        stack.local_certificates(self.root / "tls", {"api": "localhost", "identity": "localhost"})
        self.credentials = self.root / "credentials"
        self.credentials.mkdir(mode=0o700)
        for name, content in {
            "role-id": b"fixture-role-id",
            "secret-id": b"fixture-secret-id",
            "ca.pem": (self.root / "tls/identity-ca.pem").read_bytes(),
        }.items():
            path = self.credentials / name
            path.touch(mode=0o600)
            path.write_bytes(content)
        self.bao = ThreadingHTTPServer(("127.0.0.1", 0), BaoHandler)
        self.bao.paths, self.bao.policy, self.bao.available = [], "evidence-adapter", True
        self.bao.token = "fixture-receiver-token"
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(
            self.root / "tls/certificates/identity.crt", self.root / "tls/certificates/identity.key"
        )
        self.bao.socket = context.wrap_socket(self.bao.socket, server_side=True)
        self.receiver = Receiver(
            ("127.0.0.1", 0),
            self.root / "receiver.db",
            self.bao.token,
            certificate=self.root / "tls/certificates/api.crt",
            key=self.root / "tls/certificates/api.key",
        )
        for server in (self.bao, self.receiver):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.config = HttpAdapterConfig(
            id="evidence",
            receiver_id="evidence-service",
            origin=f"https://localhost:{self.receiver.server_port}",
            ca_file="ca.pem",
            timeout_seconds=3,
            contracts=[
                {
                    "shop_id": "service-example",
                    "operation_id": "request-evidence-review",
                    "command": "request-review",
                    "effect": "write",
                }
            ],
            secrets={
                "origin": f"https://localhost:{self.bao.server_port}",
                "ca_file": "ca.pem",
                "kv_mount": "chipload",
                "secret_path": "shops/service-example/evidence",
                "policy": "evidence-adapter",
            },
        )
        self.stopping = threading.Event()
        self.adapter = HttpAdapter(self.config, self.credentials, self.stopping)
        request = ExecutionRequest(
            shop_id="service-example",
            operation_id="request-evidence-review",
            expected_generation=1,
            request_key="one",
            mode="live",
            inputs=(
                Fact(name="record-reference", value="record-1"),
                Fact(name="purpose", value="prototype-verification"),
            ),
        )
        self.context = ExecutionContext(
            digest(b"execution"),
            digest(b"intent"),
            "claim",
            request,
            define_shop().operations[1],
            "owner",
            datetime.now(timezone.utc) + timedelta(seconds=30),
            False,
        )

    def test_live_tls_receipts_deduplicate_and_secret_is_not_returned(self):
        result = self.adapter.execute(self.context)
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(self.adapter.execute(self.context), result)
        self.assertEqual(self.adapter.reconcile(self.context), result)
        self.assertEqual(self.receiver.receipts(), 1)
        self.assertNotIn(self.bao.token, result.model_dump_json())
        self.assertEqual(
            self.bao.paths,
            [
                "/v1/auth/approle/login",
                "/v1/chipload/data/shops/service-example/evidence",
                "/v1/auth/token/revoke-self",
            ]
            * 3,
        )

    def test_lost_response_reconciles_without_sending_another_command(self):
        self.receiver.behavior = "lost-response"
        self.assertEqual(self.adapter.execute(self.context).state, "indeterminate")
        self.assertEqual(self.receiver.receipts(), 1)
        self.receiver.behavior = "normal"
        self.assertEqual(self.adapter.reconcile(self.context).state, "succeeded")
        self.assertEqual(self.receiver.puts, 1)
        self.assertEqual(self.receiver.gets, 1)

    def test_cancellation_is_confirmed_by_the_receiver_and_cannot_undo_completed_work(self):
        self.receiver.behavior = "pending"
        self.assertEqual(self.adapter.execute(self.context).state, "indeterminate")
        cancelled = self.adapter.reconcile(replace(self.context, cancel_requested=True))
        self.assertEqual(cancelled.state, "cancelled")
        self.assertEqual(self.receiver.cancels, 1)
        self.assertEqual(self.adapter.execute(self.context).state, "cancelled")
        self.assertEqual(self.receiver.receipts(), 1)
        self.receiver.behavior = "normal"
        completed = replace(self.context, execution_id=digest(b"completed-execution"))
        self.assertEqual(self.adapter.execute(completed).state, "succeeded")
        self.assertEqual(
            self.adapter.reconcile(replace(completed, cancel_requested=True)).state, "succeeded"
        )

    def test_registry_fingerprint_changes_fence_queued_calls_and_reconciliation(self):
        from execution_support import ExecutionFixture

        from chipload.execution_model import AdapterBinding

        f = ExecutionFixture(self.root / "chipload.db")
        adapter = f.adapters["machine"]
        adapter.fingerprint = "1" * 64
        f.execution.bind(
            AdapterBinding(
                shop_id=f.shop,
                operation_id="make-sample",
                model_sha256=f.build.report.artifact.model_sha256,
                adapter_id="machine",
                mode="simulated",
                adapter_sha256=adapter.fingerprint,
            ),
            expected_generation=1,
        )
        job = f.submit()
        adapter.fingerprint = "2" * 64
        self.assertFalse(f.runner.run_next())
        self.assertEqual(f.read(job.id).result.code, "adapter-configuration-changed")
        adapter.fingerprint = "1" * 64
        job = f.submit("second")
        context = f.runner._context(f.runner.claim_next())
        adapter.execute(context)
        f.at += timedelta(seconds=31)
        adapter.fingerprint = "2" * 64
        self.assertFalse(f.runner.reconcile(job.id))
        self.assertEqual(f.read(job.id).state, "indeterminate")
        adapter.fingerprint = "1" * 64
        self.assertTrue(f.runner.reconcile(job.id))
        self.assertEqual(f.read(job.id).state, "succeeded")

    def test_unknown_missing_mismatched_redirected_and_oversized_results_stay_unknown(self):
        for behavior in ("wrong-intent", "redirect", "oversize", "credential-output", "missing"):
            self.receiver.behavior = behavior
            with self.subTest(behavior=behavior):
                self.assertEqual(self.adapter.execute(self.context).state, "indeterminate")

    def test_credentials_and_ca_fail_before_receiver_dispatch(self):
        for change in ("unavailable", "broad-policy", "hostname"):
            self.bao.available = change != "unavailable"
            self.bao.policy = "root" if change == "broad-policy" else "evidence-adapter"
            config = self.config
            if change == "hostname":
                secret = config.secrets.model_copy(
                    update={"origin": config.secrets.origin.replace("localhost", "127.0.0.1")}
                )
                config = config.model_copy(update={"secrets": secret})
            result = HttpAdapter(config, self.credentials, self.stopping).execute(self.context)
            self.assertEqual(result.state, "rejected")
            self.assertEqual(self.receiver.puts, 0)

    def test_credential_rotation_is_used_on_the_next_call(self):
        self.assertEqual(self.adapter.execute(self.context).state, "succeeded")
        self.bao.token = self.receiver.token = "new-fixture-receiver-token"
        self.assertEqual(self.adapter.reconcile(self.context).state, "succeeded")

    def test_wall_clock_and_shutdown_bound_a_stalled_receiver(self):
        self.receiver.behavior = "stall"
        config = self.config.model_copy(update={"timeout_seconds": 1})
        start = time.monotonic()
        self.assertEqual(
            HttpAdapter(config, self.credentials, self.stopping).execute(self.context).state,
            "indeterminate",
        )
        self.assertLess(time.monotonic() - start, 2)
        self.stopping.set()
        start = time.monotonic()
        self.assertEqual(self.adapter.execute(self.context).state, "indeterminate")
        self.assertLess(time.monotonic() - start, 0.1)

    def test_network_configuration_rejects_unsafe_origins_and_secret_paths(self):
        for origin in (
            "http://localhost",
            "https://user:secret@host",
            "https://host/path",
            "https://host?query",
            "https://host/#fragment",
            "https://host:0",
        ):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                HttpAdapterConfig.model_validate({**self.config.model_dump(), "origin": origin})
        with self.assertRaises(ValueError):
            HttpAdapterConfig.model_validate(
                {
                    **self.config.model_dump(),
                    "secrets": {**self.config.secrets.model_dump(), "secret_path": "../escape"},
                }
            )

    def test_secret_reader_ignores_proxy_environment(self):
        with patch.dict(
            "os.environ", {"HTTPS_PROXY": "http://127.0.0.1:1", "ALL_PROXY": "http://127.0.0.1:1"}
        ):
            self.assertEqual(receiver_token(self.config.secrets, self.credentials), self.bao.token)

    def test_uncertain_reconciliation_backs_off_and_new_cancellation_is_prompt(self):
        clock = [0]
        with (
            patch("chipload.remote_adapter.time.monotonic", side_effect=lambda: clock[0]),
            patch.object(
                self.adapter,
                "invoke",
                return_value=AdapterResult(state="indeterminate", code="unavailable"),
            ) as invoke,
        ):
            self.assertEqual(self.adapter.reconcile(self.context).state, "indeterminate")
            clock[0] = 1
            self.assertIsNone(self.adapter.reconcile(self.context))
            self.assertEqual(invoke.call_count, 1)
            self.adapter.reconcile(replace(self.context, cancel_requested=True))
            self.assertEqual(invoke.call_count, 2)
            clock[0] = 40
            self.adapter.reconcile(replace(self.context, cancel_requested=True))
            self.assertEqual(invoke.call_count, 3)
