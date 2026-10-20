"""Python workflows against real HTTP/TLS and the existing authorization services."""

import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import uvicorn
from server_support import IdentityFixture
from test_lifecycle import FixtureBuilder
from tls_support import stack

from chipload.client import Client, HTTPTransport, ServiceError, TransportError
from chipload.lifecycle import BuildProcessor
from chipload.server import create_app
from examples.heat_treatment import SCENARIOS


class PythonClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.addCleanup(self.socket.close)
        self.origin = f"https://localhost:{self.socket.getsockname()[1]}"
        stack.local_certificates(self.root / "tls", {"api": "localhost"})
        self.ca = self.root / "tls/identity-ca.pem"
        self.identity = IdentityFixture(self.root, base=self.origin)
        self.addCleanup(self.identity.client.close)
        self.operations = self.identity.operations()
        self.server = uvicorn.Server(
            uvicorn.Config(
                create_app(
                    self.identity.settings,
                    operations=self.operations,
                    verifier=self.identity.verifier,
                ),
                ssl_certfile=self.root / "tls/certificates/api.crt",
                ssl_keyfile=self.root / "tls/certificates/api.key",
                access_log=False,
                log_level="critical",
                proxy_headers=False,
            )
        )
        thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True
        )
        thread.start()

        def stop():
            self.server.should_exit = True
            thread.join(10)
            self.assertFalse(thread.is_alive())

        self.addCleanup(stop)
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.02)
        self.assertTrue(self.server.started)
        self.client = Client.connect(self.origin, token=self.identity.token, ca_file=self.ca)
        self.shop = self.client.shop("thermal-example")

    def test_rest_code_admission_retry_inspection_and_cancellation(self):
        from program_support import SCRIPT, processor

        inputs = {"operation": "source_read", "request": {"shop_id": self.shop.id}}
        job = self.shop.run(SCRIPT, request_key="rest-program", inputs=inputs)
        self.assertEqual(job["state"], "queued")
        self.assertEqual(
            self.shop.run(SCRIPT, request_key="rest-program", inputs=inputs)["id"], job["id"]
        )
        processor(self.operations, self.identity.settings).run_next()
        result = self.shop.run_result(job["id"])
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["value"]["value"]["revision"], self.shop.source()["revision"])
        other = self.shop.run("result = 1", request_key="cancel-this")
        self.assertEqual(self.shop.cancel_run(other["id"])["state"], "cancelled")
        with self.assertRaises(ServiceError) as crossed:
            self.client.shop("other").run_result(job["id"])
        self.assertEqual(crossed.exception.status, 403)

    def test_python_authors_builds_publishes_queries_and_rolls_back(self):
        original_publication = self.shop.publication()
        original = self.shop.source()
        project = original["project"]
        project["files"][0]["content"] += "\n# Edited from a Python client\n"
        saved = self.shop.save(project, expected_head=original["revision"])
        job = self.shop.build(saved["revision"], request_key="python-edit")
        self.assertEqual(
            self.shop.build(saved["revision"], request_key="python-edit")["id"], job["id"]
        )
        BuildProcessor(self.operations.service.repository, FixtureBuilder()).run(job["id"])
        result = self.shop.build_result(job["id"])
        self.assertEqual(result["state"], "succeeded")
        self.assertTrue(all(case["passed"] for case in result["report"]["tests"]))
        published = self.shop.publish(
            job["id"], expected_generation=original_publication["generation"]
        )
        self.assertEqual(self.shop.catalog()["shop"]["id"], self.shop.id)
        self.assertEqual(
            self.shop.rollback(
                original_publication["build_id"], expected_generation=published["generation"]
            )["build_id"],
            original_publication["build_id"],
        )
        with self.assertRaises(ServiceError) as stale:
            self.shop.save(project, expected_head=original["revision"])
        self.assertEqual(stale.exception.status, 409)
        self.assertEqual(self.shop.source()["revision"], saved["revision"])
        self.assertTrue(self.shop.audit()["events"])

    def test_public_queries_compose_in_python_and_match_private_scope(self):
        public = Client.connect(self.origin, ca_file=self.ca).shop(self.shop.id)
        results = {
            case.name: self.shop.evaluate(case.question, private=True)["outcome"]
            for case in SCENARIOS
        }
        self.assertEqual(results, {case.name: case.expected for case in SCENARIOS})
        self.assertEqual(public.catalog(), self.shop.catalog())
        page = public.page("capabilities", limit=1)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(
            public.inspect("capabilities", page["items"][0]["id"])["item"], page["items"][0]
        )
        self.assertIn("variables", public.variables({}))
        with self.assertRaises(ServiceError) as denied:
            public.source()
        self.assertEqual(denied.exception.status, 401)
        with self.assertRaises(ServiceError) as crossed:
            self.client.shop("another-shop").source()
        self.assertEqual(crossed.exception.status, 403)

    def test_editor_can_read_source_but_cannot_publish_or_request_private_evidence(self):
        client = Client.connect(
            self.origin,
            ca_file=self.ca,
            token=lambda: self.identity.token("editor", roles=("shop:admin",)),
        )
        shop = client.shop(self.shop.id)
        self.assertTrue(shop.source()["revision"])
        for read in (shop.publication, shop.evidence):
            with self.assertRaises(ServiceError) as denied:
                read()
            self.assertEqual(denied.exception.status, 403)

    def test_token_provider_is_read_again_and_errors_do_not_print_tokens(self):
        token = [self.identity.token()]
        client = Client.connect(self.origin, token=lambda: token[0], ca_file=self.ca)
        self.assertEqual(client.identity()["subject"], "owner")
        token[0] = self.identity.token("outsider")
        with self.assertRaises(ServiceError) as denied:
            client.shop(self.shop.id).source()
        self.assertEqual(denied.exception.status, 403)
        self.assertNotIn(token[0], str(denied.exception))

    def test_tls_name_and_chain_fail_and_proxy_environment_is_ignored(self):
        for origin, ca in (
            (self.origin, None),
            (self.origin.replace("localhost", "127.0.0.1"), self.ca),
        ):
            with self.subTest(origin=origin), self.assertRaises(TransportError):
                Client.connect(origin, ca_file=ca).shop(self.shop.id).catalog()
        with patch.dict(
            "os.environ", {"HTTPS_PROXY": "http://127.0.0.1:1", "ALL_PROXY": "http://127.0.0.1:1"}
        ):
            self.assertEqual(self.client.identity()["subject"], "owner")

    def test_origins_and_path_components_cannot_redirect_credentials(self):
        for origin in (
            "http://example.com",
            "http://127.0.0.1",
            "https://user:password@host",
            "https://host/other",
            "https://host?token=secret",
        ):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                HTTPTransport(origin)
        HTTPTransport("http://127.0.0.1", allow_http_loopback=True)
        for value in ("../identity", "thermal-example?other", "thermal-example/source"):
            with self.assertRaises(ValueError):
                self.client.shop(value)
        with self.assertRaises(ValueError):
            self.shop.inspect("capabilities", "../source")
