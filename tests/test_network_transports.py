"""Actual local/remote HTTPS: authenticated SDK jobs cross the trusted connector."""

import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from cloud_support import CloudServer
from peer_support import PeerServer

from chipload.access import Grant, Principal
from chipload.client import Client, ServiceError
from chipload.connector import CloudLink, Connector, ConnectorSettings, DispatchLink
from chipload.connector_auth import WorkloadIdentity
from chipload.directory_model import Advertisement, SupplierSearch
from chipload.peer_model import PeerCatalog, PeerListing, PeerRequest
from examples.federation_shops import query, term


class NetworkFixture:
    def setUp(self):
        agent = Principal(subject="agent", grants=(Grant(shop_id="mill", actions=("network:query", "code:run", "code:read", "code:cancel")),))
        dispatcher = Principal(subject="dispatcher", grants=(Grant(shop_id="mill", actions=("network:dispatch",)),))
        self.buyer = PeerServer(profile="mill", bindings=(agent, dispatcher))
        self.addCleanup(self.buyer.close)
        self.supplier = PeerServer()
        self.addCleanup(self.supplier.close)
        self.client = Client.connect(self.buyer.origin, ca_file=self.buyer.policy.ca_file,
            token=lambda: self.buyer.identity.token("agent", roles=("shop:operate",)))
        self.shop = self.client.shop("mill")
        self.dispatch = DispatchLink(credentials=self.buyer.root / "dispatch-credentials",
            identity=WorkloadIdentity(client_id="local-dispatch", subject="dispatcher",
                trust=self.buyer.settings.model_copy(update={"bindings": (), "instance_id": None}), policy=self.buyer.policy,
                secrets={"origin": self.buyer.origin, "kv_mount": "connector", "secret_path": "local/client", "policy": "local-dispatch"}))
        self.settings = ConnectorSettings(instance_id=self.buyer.settings.instance_id,
            source_origin=self.buyer.origin, source_policy=self.buyer.policy, query_policy=self.supplier.policy,
            shops=("mill",), dispatch=self.dispatch)

    def connector(self, settings=None, cloud=None):
        instance = Connector(settings or self.settings,
            dispatch_token_factory=lambda _: lambda: self.buyer.identity.token("dispatcher", roles=("shop:connect",)),
            token_factory=(lambda _: cloud.owner(0).token) if cloud else None)
        self.addCleanup(instance.close)
        instance.start()
        return instance

    def wait(self, identifier):
        until = time.monotonic() + 15
        while time.monotonic() < until:
            record = self.shop.network_result(identifier)
            if record["state"] not in {"queued", "running"}:
                return record
            time.sleep(.1)
        self.fail("network delivery did not finish")

    def request(self, peer_query):
        return PeerRequest(instance_id=self.supplier.settings.instance_id, request_id=uuid.uuid4().hex,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=30), query=peer_query)

class NetworkTransportTests(NetworkFixture, unittest.TestCase):
    def test_direct_jobs_can_start_when_a_configured_public_directory_is_offline(self):
        cloud = CloudServer((self.supplier,))
        self.addCleanup(cloud.close)
        link = CloudLink(id="offline", origin=cloud.origin, directory_id=cloud.settings.directory_id,
                         policy=cloud.policy, advertise=False, relay=False)
        cloud.stop()
        self.connector(self.settings.model_copy(update={"clouds": (link,)}))
        envelope = self.request(PeerCatalog(shop_id="supplier-far"))
        receipt = self.shop.network({"kind": "direct", "origin": self.supplier.origin, "request": envelope},
            deadline=envelope.deadline, request_key="direct-with-cloud-offline")
        self.assertEqual(self.wait(receipt["id"])["state"], "succeeded")

    def test_conventional_rest_and_python_compose_direct_reads_without_a_cloud(self):
        self.connector()
        envelope = self.request(PeerCatalog(shop_id="supplier-far"))
        receipt = self.shop.network({"kind": "direct", "origin": self.supplier.origin, "request": envelope},
            deadline=envelope.deadline, request_key="catalog")
        result = self.wait(receipt["id"])
        self.assertEqual(result["state"], "succeeded", result)
        self.assertEqual(result["result"]["result"]["value"], self.supplier.client.catalog("supplier-far").value)
        self.assertEqual(self.shop.network({"kind": "direct", "origin": self.supplier.origin, "request": envelope},
            deadline=envelope.deadline, request_key="catalog")["id"], receipt["id"])
        # Grant scope is enforced before an untrusted completion body is parsed.
        with self.assertRaises(ServiceError) as forbidden:
            self.client.call("network_claim", {"shop_id": "mill"})
        self.assertEqual(forbidden.exception.status, 403)
        with self.assertRaises(ServiceError) as unparsed:
            self.client.call("network_complete", {"shop_id": "mill", "unexpected": True})
        self.assertEqual(unparsed.exception.status, 403)
        with self.assertRaises(ServiceError) as cross_shop:
            self.client.shop("supplier-far").network_result(receipt["id"])
        self.assertEqual(cross_shop.exception.status, 403)

    def test_queued_directory_and_relay_queries_share_fresh_supplier_business_results(self):
        cloud = CloudServer((self.supplier,))
        self.addCleanup(cloud.close)
        catalog = self.supplier.client.catalog("supplier-far")
        cloud.owner(0).advertise(self.supplier.settings.instance_id, "supplier-far", Advertisement(
            request_id=uuid.uuid4().hex, expected_version=0, observed_at=datetime.now(timezone.utc),
            origin=self.supplier.origin, catalog=catalog))
        link = CloudLink(id="cloud", origin=cloud.origin, directory_id=cloud.settings.directory_id,
            credentials=cloud.root / "credentials", advertise=False, identity=WorkloadIdentity(
                client_id="cloud-connector", subject="owner-0", trust=cloud.settings.identity,
                policy=cloud.policy, secrets={"origin": cloud.origin, "kv_mount": "connector", "secret_path": "cloud/client", "policy": "cloud-connector"}))
        # The supplier owns its own outbound connection, distinct from the buyer.
        supplier_connector = Connector(ConnectorSettings(instance_id=self.supplier.settings.instance_id,
            source_origin=self.supplier.origin, source_policy=self.supplier.policy, clouds=(link,)),
            token_factory=lambda _: cloud.owner(0).token)
        self.addCleanup(supplier_connector.close)
        supplier_connector.start()
        until = time.monotonic() + 8
        while self.supplier.settings.instance_id not in cloud.app.state.broker.connected() and time.monotonic() < until:
            time.sleep(.05)
        # Buyer uses the cloud only for public queries; it never enrolls as the supplier.
        buyer_link = CloudLink(id="cloud", origin=cloud.origin, directory_id=cloud.settings.directory_id,
                              policy=cloud.policy, advertise=False, relay=False)
        connector = self.connector(self.settings.model_copy(update={"clouds": (buyer_link,)}), cloud)
        self.assertTrue(connector.threads)
        deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
        receipt = self.shop.network({"kind": "search", "cloud": "cloud", "directory_id": cloud.settings.directory_id,
            "query": SupplierSearch(profile=term("material-supply"))}, deadline=deadline, request_key="search")
        page = self.wait(receipt["id"])
        self.assertEqual(page["state"], "succeeded", page)
        candidate = page["result"]["candidates"][0]
        envelope = self.request(PeerListing(shop_id="supplier-far", listing_id="service",
            expected_generation=candidate["source_generation"], query=query(revision=candidate["source_revision"])))
        receipt = self.shop.network({"kind": "relay", "cloud": "cloud", "request": envelope},
            deadline=envelope.deadline, request_key="evaluate")
        result = self.wait(receipt["id"])
        self.assertEqual(result["state"], "succeeded", result)
        self.assertEqual(result["result"]["result"]["value"]["outcome"], "supported")
        self.supplier.service.withdraw(self.supplier.owner, "supplier-far", expected_generation=1)
        envelope = self.request(PeerCatalog(shop_id="supplier-far"))
        receipt = self.shop.network({"kind": "relay", "cloud": "cloud", "request": envelope},
            deadline=envelope.deadline, request_key="withdrawn")
        refused = self.wait(receipt["id"])
        self.assertEqual(refused["state"], "failed")
        self.assertEqual(refused["error"]["status"], 404)
