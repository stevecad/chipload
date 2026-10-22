"""Managed outbound sessions and publication sync over actual HTTPS listeners."""

import sqlite3
import time
import unittest
from unittest.mock import Mock, patch

from cloud_support import CloudServer
from peer_support import PeerServer

from chipload.client import ServiceError, TransportError
from chipload.connector import CloudLink, Connector, ConnectorSettings
from chipload.connector_auth import WorkloadIdentity
from chipload.directory_model import SupplierSearch
from examples.federation_shops import define_shop, query, term


def admitted_read(read):
    """A fixture may repeat a read explicitly refused before request admission."""
    for attempt in range(4):
        try:
            return read()
        except ServiceError as error:
            if (attempt == 3 or error.status != 503 or error.code != "request_capacity"
                    or type(error.retry_after) is not int or not 1 <= error.retry_after <= 5):
                raise
            time.sleep(error.retry_after)
    raise AssertionError("unreachable")


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.peer = PeerServer()
        self.addCleanup(self.peer.close)
        self.cloud = CloudServer((self.peer,))
        self.addCleanup(self.cloud.close)
        self.link = CloudLink(id="cloud", origin=self.cloud.origin, directory_id=self.cloud.settings.directory_id,
            credentials=self.cloud.root / "credentials", identity=WorkloadIdentity(
                client_id="connector", subject="owner-0", trust=self.cloud.settings.identity,
                policy=self.cloud.policy, secrets={"origin": self.cloud.origin, "kv_mount": "connector",
                    "secret_path": "cloud/client", "field": "client-secret", "policy": "connector"}))
        self.settings = ConnectorSettings(instance_id=self.peer.settings.instance_id,
            source_origin=self.peer.origin, source_policy=self.peer.policy,
            shops=("supplier-far",), clouds=(self.link,))

    def connector(self, settings=None):
        # Credential exchange is separately exercised by test_connector_auth.py;
        # this fixture supplies signed owner JWTs to the real network client.
        connector = Connector(settings or self.settings, token_factory=lambda _: self.cloud.owner(0).token)
        self.addCleanup(connector.close)
        return connector

    def wait(self, predicate):
        until = time.monotonic() + 12
        while time.monotonic() < until:
            value = predicate()
            if value:
                return value
            time.sleep(.05)
        self.fail("connector fixture did not reach the expected state")

    def candidates(self):
        return admitted_read(lambda: self.cloud.client.search(SupplierSearch(profile=term("material-supply")))).candidates

    def test_outbound_startup_withdrawal_restart_and_cloud_outage(self):
        connector = self.connector()
        connector.start()
        candidate = self.wait(self.candidates)[0]
        self.assertIsNone(candidate.direct_origin)
        self.assertTrue(candidate.relay_available)
        result = admitted_read(lambda: self.cloud.client.evaluate_listing(self.peer.settings.instance_id,
            "supplier-far", "service", query(revision=candidate.source_revision), expected_generation=1))
        self.assertEqual(result.value["outcome"], "supported")
        self.peer.service.withdraw(self.peer.owner, "supplier-far", expected_generation=1)
        connector.refresh("cloud")
        self.assertFalse(self.candidates())
        self.assertIsNone(admitted_read(lambda: self.cloud.owner(0).head(self.peer.settings.instance_id, "supplier-far")).expires_at)
        with self.assertRaises(ServiceError) as withdrawn:
            admitted_read(lambda: self.cloud.client.catalog(self.peer.settings.instance_id, "supplier-far"))
        self.assertEqual(withdrawn.exception.status, 404)
        self.peer.build(define_shop())
        connector.refresh("cloud")
        candidate = self.candidates()[0]
        self.assertGreater(candidate.source_generation, 1)
        old_epoch = connector.sessions["cloud"].epoch
        self.cloud.restart()
        self.wait(lambda: connector.sessions.get("cloud") is not None and connector.sessions["cloud"].epoch > old_epoch)
        self.assertEqual(admitted_read(lambda: self.cloud.client.catalog(self.peer.settings.instance_id, "supplier-far")).generation,
                         candidate.source_generation)
        self.cloud.stop()
        self.assertEqual(self.peer.client.catalog("supplier-far").generation, candidate.source_generation)
        connector.close()
        self.assertTrue(all(not thread.is_alive() for thread in connector.threads))

    def test_direct_publication_can_run_without_an_outbound_relay(self):
        settings = self.settings.model_copy(update={"public_origin": self.peer.origin,
            "clouds": (self.link.model_copy(update={"relay": False}),)})
        connector = self.connector(settings)
        connector.start()
        candidate = self.wait(self.candidates)[0]
        self.assertEqual(candidate.direct_origin, self.peer.origin)
        self.assertFalse(candidate.relay_available)
        self.assertEqual(self.cloud.app.state.broker.connected(), frozenset())

    def test_fixture_read_recovers_after_real_accounting_lock_refusal(self):
        path = self.cloud.settings.identity.database.with_name("cloud.db.limits.admission")
        refused = []
        with sqlite3.connect(path) as writer:
            writer.execute("BEGIN IMMEDIATE")

            def read():
                try:
                    return self.cloud.client.search(SupplierSearch(profile=term("material-supply")))
                except ServiceError as error:
                    refused.append((error.status, error.code))
                    writer.rollback()
                    raise

            self.assertEqual(admitted_read(read).candidates, ())
        self.assertEqual(refused, [(503, "request_capacity")])

    def test_source_identity_mismatch_stops_before_cloud_enrollment(self):
        connector = self.connector(self.settings.model_copy(update={"instance_id": "0" * 32}))
        with self.assertRaises(TransportError):
            connector.start()
        self.assertFalse(connector.threads)
        with self.cloud.app.state.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM epochs").fetchone()[0], 0)


class ConnectorAdmissionTests(unittest.TestCase):
    def refusal(self, code="request_capacity", *, status=503, delay=1):
        return ServiceError(status, {"error": {"code": code, "retry_after": delay}})

    def test_read_recovers_from_explicit_pre_admission_refusal(self):
        read = Mock(side_effect=[self.refusal(), {"generation": 3}])
        with patch("test_connector.time.sleep") as sleep:
            self.assertEqual(admitted_read(read), {"generation": 3})
        self.assertEqual(read.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_unrelated_refusals_transport_errors_and_invalid_retry_metadata_are_not_retried(self):
        errors = [self.refusal(code) for code in ("relay_offline", "relay_capacity", "relay_session_replaced", "identity_unavailable")]
        errors += [self.refusal(status=429), TransportError("lost reply")]
        errors += [self.refusal(delay=delay) for delay in (None, True, 0, 6, "1")]
        for error in errors:
            with self.subTest(error=type(error).__name__, detail=getattr(error, "detail", None)):
                read = Mock(side_effect=error)
                with patch("test_connector.time.sleep") as sleep, self.assertRaises(type(error)):
                    admitted_read(read)
                self.assertEqual(read.call_count, 1)
                sleep.assert_not_called()

    def test_persistent_refusal_keeps_a_fixed_attempt_bound(self):
        error = self.refusal()
        read = Mock(side_effect=error)
        with patch("test_connector.time.sleep") as sleep, self.assertRaises(ServiceError) as caught:
            admitted_read(read)
        self.assertIs(caught.exception, error)
        self.assertEqual(read.call_count, 4)
        self.assertEqual(sleep.call_count, 3)
