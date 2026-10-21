"""Direct TLS, identity/revision fencing, privacy and bounded network authority."""

import socket
import subprocess
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from peer_support import PeerServer
from server_support import IdentityFixture

from chipload.client import ServiceError, TransportError
from chipload.federation_wire import (
    EgressPolicy,
    WireJob,
    checked_origin,
    request,
    resolved_addresses,
)
from chipload.peer import PeerClient
from chipload.peer_model import PeerCatalog, PeerEvaluate, PeerRequest, PeerResponse, PeerVariables
from examples.federation_shops import cases, define_shop, query


class PeerTests(unittest.TestCase):
    def setUp(self):
        self.peer = PeerServer()
        self.addCleanup(self.peer.close)

    def test_actual_https_catalog_listing_capability_and_variables_share_domain_results(self):
        self.assertEqual(self.peer.client.descriptor().instance_id, self.peer.settings.instance_id)
        catalog = self.peer.client.catalog("supplier-far")
        self.assertEqual(catalog.generation, 1)
        self.assertEqual(catalog.value, self.peer.published.catalog("supplier-far"))
        listing_query = query(revision=catalog.revision)
        result = self.peer.client.evaluate_listing("supplier-far", "service", listing_query, expected_generation=1)
        self.assertEqual(result.value, self.peer.published.listing_evaluation("supplier-far", "service", listing_query))
        evaluation = cases("supplier-far")[0].request
        result = self.peer.client.query(PeerEvaluate(shop_id="supplier-far", expected_revision=catalog.revision, query=evaluation))
        self.assertEqual(result.value, self.peer.published.evaluation("supplier-far", evaluation))
        result = self.peer.client.query(PeerVariables(shop_id="supplier-far", expected_revision=catalog.revision, query={}))
        self.assertEqual(result.revision, catalog.revision)

    def test_independent_instances_can_share_local_shop_ids(self):
        other = PeerServer()
        self.addCleanup(other.close)
        self.assertNotEqual(other.settings.instance_id, self.peer.settings.instance_id)
        self.assertEqual(other.client.catalog("supplier-far").value, self.peer.client.catalog("supplier-far").value)
        wrong = PeerClient(self.peer.origin, other.settings.instance_id, policy=self.peer.policy)
        with self.assertRaises(TransportError):
            wrong.descriptor()
        with self.assertRaises(ServiceError) as refused:
            wrong.catalog("supplier-far")
        self.assertEqual(refused.exception.status, 404)

    def test_publication_generation_fences_rollback_even_with_identical_public_catalog(self):
        original = self.peer.client.catalog("supplier-far")
        self.peer.build(define_shop())
        current = self.peer.client.catalog("supplier-far")
        self.assertEqual(current.revision, original.revision)
        self.assertEqual(current.generation, 2)
        with self.assertRaises(ServiceError) as changed:
            self.peer.client.evaluate_listing("supplier-far", "service", query(revision=original.revision), expected_generation=1)
        self.assertEqual(changed.exception.status, 409)
        self.peer.service.withdraw(self.peer.owner, "supplier-far", expected_generation=2)
        with self.assertRaises(ServiceError) as withdrawn:
            self.peer.client.catalog("supplier-far")
        self.assertEqual(withdrawn.exception.status, 404)

    def test_deadlines_and_privileged_or_arbitrary_requests_are_rejected(self):
        envelope = PeerRequest(instance_id=self.peer.settings.instance_id, request_id=uuid.uuid4().hex,
                               deadline=datetime.now(timezone.utc) - timedelta(seconds=1),
                               query=PeerCatalog(shop_id="supplier-far")).model_dump(mode="json")
        for data in (envelope, {**envelope, "query": {"action": "source_read", "shop_id": "supplier-far"}},
                     {**envelope, "query": {"action": "catalog", "shop_id": "supplier-far", "url": "https://other.example"}}):
            with self.assertRaises(ServiceError) as rejected:
                request(self.peer.origin, "POST", "/v1/peer/query", policy=self.peer.policy, body=data)
            self.assertEqual(rejected.exception.status, 422)

    def test_reply_identity_action_content_and_revision_are_checked(self):
        catalog = self.peer.client.catalog("supplier-far")
        def malicious(origin, method, path, *, body, **kwargs):
            value = PeerResponse(instance_id=body["instance_id"], request_id=body["request_id"], result=catalog).model_dump(mode="json")
            change(value)
            return value
        mutations = (
            lambda r: r.update(instance_id="0" * 32),
            lambda r: r.update(request_id="0" * 32),
            lambda r: r["result"].update(generation=3),
            lambda r: r["result"]["value"]["shop"].update(name="Forged content"),
            lambda r: r["result"].update(action="evaluate"),
        )
        for change in mutations:
            with patch("chipload.peer.request", side_effect=malicious), self.assertRaises(TransportError):
                self.peer.client.catalog("supplier-far", expected_generation=1)

    def test_untrusted_tls_and_private_endpoints_fail_before_any_query(self):
        with self.assertRaises(TransportError):
            PeerClient(self.peer.origin, self.peer.settings.instance_id).catalog("supplier-far")
        policy = EgressPolicy(private_origins=(self.peer.origin,), seconds=3)
        with self.assertRaises(TransportError):
            PeerClient(self.peer.origin, self.peer.settings.instance_id, policy=policy).catalog("supplier-far")

    def test_peer_calls_cross_the_actual_supervised_service_process(self):
        managed = PeerServer(managed=True)
        self.addCleanup(managed.close)
        catalog = managed.client.catalog("supplier-far")
        result = managed.client.evaluate_listing("supplier-far", "service", query(revision=catalog.revision), expected_generation=1)
        self.assertEqual(result.value["outcome"], "supported")


class DisabledPeerTests(unittest.TestCase):
    def test_existing_configs_do_not_enable_peer_identity_or_routes(self):
        from chipload.server import create_app

        with tempfile.TemporaryDirectory() as directory:
            identity = IdentityFixture(directory)
            self.addCleanup(identity.client.close)
            self.assertNotIn("instance_id", identity.settings.model_dump(mode="json"))
            with TestClient(create_app(identity.settings, operations=identity.operations(), verifier=identity.verifier), base_url=identity.settings.base_url) as client:
                self.assertEqual(client.get("/.well-known/chipload").status_code, 404)
                request = PeerRequest(instance_id="0" * 32, request_id=uuid.uuid4().hex,
                                      deadline=datetime.now(timezone.utc) + timedelta(seconds=10),
                                      query=PeerCatalog(shop_id="thermal-example"))
                self.assertEqual(client.post("/v1/peer/query", json=request.model_dump(mode="json")).status_code, 404)


class EgressTests(unittest.TestCase):
    def answer(self, host, *, family=socket.AF_INET):
        address = (host, 443, 0, 0) if family == socket.AF_INET6 else (host, 443)
        return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", address)

    def test_every_dns_answer_is_checked_including_ipv4_mapped_ipv6(self):
        public = self.answer("93.184.216.34")
        for address, family in (("127.0.0.1", socket.AF_INET), ("10.0.0.1", socket.AF_INET),
                                ("169.254.169.254", socket.AF_INET), ("::ffff:127.0.0.1", socket.AF_INET6),
                                ("::1", socket.AF_INET6), ("0.0.0.0", socket.AF_INET)):
            with patch("socket.getaddrinfo", return_value=[public, self.answer(address, family=family)]):
                with self.assertRaises(ValueError):
                    resolved_addresses("https://public.example", EgressPolicy())
        with patch("socket.getaddrinfo", return_value=[public]):
            self.assertEqual(resolved_addresses("https://public.example", EgressPolicy()), [(socket.AF_INET, socket.IPPROTO_TCP, ("93.184.216.34", 443))])

    def test_private_exception_is_exact_and_metadata_remains_blocked(self):
        policy = EgressPolicy(private_origins=("https://lan.example:8443",))
        with patch("socket.getaddrinfo", return_value=[self.answer("10.0.0.1")]):
            self.assertTrue(resolved_addresses("https://lan.example:8443", policy))
            for other in ("https://other.example:8443", "https://lan.example"):
                with self.assertRaises(ValueError):
                    resolved_addresses(other, policy)
        with patch("socket.getaddrinfo", return_value=[self.answer("169.254.169.254")]):
            with self.assertRaises(ValueError):
                resolved_addresses("https://lan.example:8443", policy)

    def test_scheme_credentials_paths_and_nonliteral_headers_are_not_accepted(self):
        for origin in ("http://example.org", "https://name:secret@example.org", "https://example.org/path", "https://example.org?x=1", "https://example.org/#part"):
            with self.assertRaises(ValueError):
                checked_origin(origin)
        for path in ("//evil.example/", "/../secret?x=1", "https://evil.example", "/path\r\nHeader:x"):
            with self.assertRaises(ValueError):
                WireJob(origin="https://example.org", policy=EgressPolicy(), method="GET", path=path)

    def test_process_deadline_is_not_retried(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("worker", 3)) as run:
            with self.assertRaises(TransportError):
                request("https://example.org", "GET", "/.well-known/chipload", policy=EgressPolicy(seconds=1))
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.kwargs["timeout"], 3)

    def test_connections_use_validated_addresses_without_a_second_resolution(self):
        from chipload.federation_wire import PinnedHTTPS

        address = ("93.184.216.34", 443)
        from unittest.mock import Mock
        context, sock = Mock(), Mock()
        with patch("socket.socket", return_value=sock), patch("socket.getaddrinfo", side_effect=AssertionError("second DNS")):
            connection = PinnedHTTPS("supplier.example", 443, addresses=[(socket.AF_INET, socket.IPPROTO_TCP, address)], context=context, timeout=2)
            connection.connect()
        sock.connect.assert_called_once_with(address)
        context.wrap_socket.assert_called_once_with(sock, server_hostname="supplier.example")
