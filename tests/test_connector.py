"""Managed outbound sessions and publication sync over actual HTTPS listeners."""

import time
import unittest

from cloud_support import CloudServer
from peer_support import PeerServer

from chipload.client import ServiceError, TransportError
from chipload.connector import CloudLink, Connector, ConnectorSettings
from chipload.connector_auth import WorkloadIdentity
from chipload.directory_model import SupplierSearch
from examples.federation_shops import define_shop, query, term


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
        return self.cloud.client.search(SupplierSearch(profile=term("material-supply"))).candidates

    def test_outbound_startup_withdrawal_restart_and_cloud_outage(self):
        connector = self.connector()
        connector.start()
        candidate = self.wait(self.candidates)[0]
        self.assertIsNone(candidate.direct_origin)
        self.assertTrue(candidate.relay_available)
        result = self.cloud.client.evaluate_listing(self.peer.settings.instance_id,
            "supplier-far", "service", query(revision=candidate.source_revision), expected_generation=1)
        self.assertEqual(result.value["outcome"], "supported")
        self.peer.service.withdraw(self.peer.owner, "supplier-far", expected_generation=1)
        connector.refresh("cloud")
        self.assertFalse(self.candidates())
        self.assertIsNone(self.cloud.owner(0).head(self.peer.settings.instance_id, "supplier-far").expires_at)
        with self.assertRaises(ServiceError) as withdrawn:
            self.cloud.client.catalog(self.peer.settings.instance_id, "supplier-far")
        self.assertEqual(withdrawn.exception.status, 404)
        self.peer.build(define_shop())
        connector.refresh("cloud")
        candidate = self.candidates()[0]
        self.assertGreater(candidate.source_generation, 1)
        old_epoch = connector.sessions["cloud"].epoch
        self.cloud.restart()
        self.wait(lambda: connector.sessions.get("cloud") is not None and connector.sessions["cloud"].epoch > old_epoch)
        self.assertEqual(self.cloud.client.catalog(self.peer.settings.instance_id, "supplier-far").generation,
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

    def test_source_identity_mismatch_stops_before_cloud_enrollment(self):
        connector = self.connector(self.settings.model_copy(update={"instance_id": "0" * 32}))
        with self.assertRaises(TransportError):
            connector.start()
        self.assertFalse(connector.threads)
        with self.cloud.app.state.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM epochs").fetchone()[0], 0)
