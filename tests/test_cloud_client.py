"""End-to-end HTTPS directory search and outbound polling through the Python client."""

import threading
import time
import unittest
import uuid
from datetime import datetime, timezone

from cloud_support import CloudServer
from peer_support import PeerServer

from chipload.client import ServiceError, TransportError
from chipload.cloud_client import CloudClient
from chipload.directory_model import Advertisement, SupplierSearch
from examples.federation_shops import query, term


class Forwarder:
    """Test receiver; production connector credential/lifecycle management is separate."""

    def __init__(self, owner, peer):
        self.owner, self.peer = owner, peer
        self.session = owner.open(peer.settings.instance_id)
        self.stop = threading.Event()
        self.errors = []
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            try:
                delivery = self.owner.poll(self.session).delivery
                if delivery is None:
                    continue
                try:
                    response = self.peer.client.forward(delivery.request)
                except ServiceError as error:
                    self.owner.complete(self.session, delivery.delivery_id, error={"status": error.status, "code": error.code})
                else:
                    self.owner.complete(self.session, delivery.delivery_id, response=response)
            except Exception as error:
                if not self.stop.is_set():
                    self.errors.append(type(error).__name__)
                break

    def close(self):
        if self.stop.is_set():
            return
        self.stop.set()
        try:
            self.owner.close(self.session)
        except (ServiceError, TransportError):
            pass
        self.thread.join(18)
        if self.thread.is_alive():
            raise RuntimeError("test relay receiver did not stop")


class CloudClientTests(unittest.TestCase):
    def setUp(self):
        self.near = PeerServer(profile="supplier-near")
        self.addCleanup(self.near.close)
        self.far = PeerServer()
        self.addCleanup(self.far.close)
        self.cloud = CloudServer((self.near, self.far))
        self.addCleanup(self.cloud.close)

    def advertise(self, peer, owner, *, direct=True):
        catalog = peer.client.catalog(peer.profile)
        ad = Advertisement(request_id=uuid.uuid4().hex, expected_version=0,
            observed_at=datetime.now(timezone.utc), origin=peer.origin if direct else None, catalog=catalog)
        return owner.advertise(peer.settings.instance_id, peer.profile, ad)

    def test_search_nearest_then_evaluate_business_rules_over_direct_and_relay_https(self):
        self.assertEqual(self.cloud.client.descriptor().directory_id, self.cloud.settings.directory_id)
        self.advertise(self.near, self.cloud.owner(0))
        owner = self.cloud.owner(1)
        receiver = Forwarder(owner, self.far)
        self.addCleanup(receiver.close)
        self.advertise(self.far, owner, direct=False)
        search = SupplierSearch(profile=term("material-supply"), inputs=query(revision="a" * 64).inputs,
            origin={"latitude": "41.88", "longitude": "-87.63", "accuracy_m": 50}, radius_m=100_000)
        page = self.cloud.client.search(search)
        self.assertEqual([candidate.shop_id for candidate in page.candidates], ["supplier-near", "supplier-far"])
        first, second = page.candidates
        direct = self.near.client.evaluate_listing(first.shop_id, first.listing.id,
            query("supplier-near", revision=first.source_revision), expected_generation=first.source_generation)
        relayed = self.cloud.client.evaluate_listing(second.instance_id, second.shop_id, second.listing.id,
            query(revision=second.source_revision), expected_generation=second.source_generation)
        self.assertEqual([direct.value["outcome"], relayed.value["outcome"]], ["unsupported", "supported"])
        self.assertEqual(relayed, self.far.client.evaluate_listing("supplier-far", "service", query(revision=second.source_revision), expected_generation=1))
        self.assertFalse(receiver.errors)
        receiver.close()
        self.cloud.stop()
        self.assertEqual(self.near.client.catalog("supplier-near").generation, 1)
        with self.assertRaises(TransportError):
            self.cloud.client.catalog(self.far.settings.instance_id, "supplier-far")

    def test_ownership_restart_and_stale_session_rejection_preserve_directory_history(self):
        owner = self.cloud.owner(0)
        receipt = self.advertise(self.near, owner)
        session = owner.open(self.near.settings.instance_id)
        self.cloud.restart()
        self.assertEqual(owner.head(self.near.settings.instance_id, "supplier-near"), receipt)
        with self.assertRaises(ServiceError) as stale:
            owner.poll(session)
        self.assertEqual(stale.exception.status, 409)
        newer = owner.open(self.near.settings.instance_id)
        self.assertGreater(newer.epoch, session.epoch)
        with self.assertRaises(ServiceError) as denied:
            self.cloud.owner(1).head(self.near.settings.instance_id, "supplier-near")
        self.assertEqual(denied.exception.status, 403)

    def test_idle_outbound_poll_fits_within_client_deadline(self):
        original = self.cloud.owner(0)
        owner = CloudClient(original.origin, original.directory_id,
                            policy=original.policy.model_copy(update={"seconds": 4}), token=original.token)
        session = owner.open(self.near.settings.instance_id)
        self.addCleanup(owner.close, session)
        start = time.monotonic()
        self.assertIsNone(owner.poll(session).delivery)
        self.assertLess(time.monotonic() - start, owner.policy.seconds)
