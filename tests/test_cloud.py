"""Real JWT ownership, source-verified directory updates and outbound relay HTTP."""

import asyncio
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from peer_support import PeerServer
from server_support import IdentityFixture

from chipload.client import ServiceError
from chipload.cloud import create_cloud_app, initialize_cloud
from chipload.directory import DirectoryStore
from chipload.directory_model import Advertisement, CloudSettings, OwnerBinding, SupplierSearch
from chipload.peer_model import PeerCatalog, PeerListing, PeerRequest, PeerResponse
from chipload.relay import CompleteRelay, PollResult, RelayBroker, RelaySession
from examples.federation_shops import query, term


class CloudTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.first, self.second = "a" * 32, "b" * 32
        self.source = await asyncio.to_thread(PeerServer, instance_id=self.first)
        self.identity = IdentityFixture(self.temporary.name, base="https://cloud.example")
        identity = self.identity.settings.model_copy(update={"database": Path(self.temporary.name) / "private/cloud.db", "bindings": ()})
        self.settings = CloudSettings(directory_id="c" * 32, identity=identity,
            egress=self.source.policy,
            bindings=(OwnerBinding(subject="owner-a", instance_id=self.first), OwnerBinding(subject="owner-b", instance_id=self.second)))
        self.now = datetime.now(timezone.utc)
        self.store = initialize_cloud(self.settings, clock=lambda: self.now)
        self.broker = RelayBroker(poll_seconds=.1)
        self.app = create_cloud_app(self.settings, store=self.store, verifier=self.identity.verifier, broker=self.broker)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url="https://cloud.example")

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.lifespan.__aexit__(None, None, None)
        await asyncio.to_thread(self.source.close)
        self.identity.client.close()
        self.temporary.cleanup()

    def auth(self, subject="owner-a", **kwargs):
        return {"Authorization": "Bearer " + self.identity.token(subject, **kwargs)}

    async def advertisement(self, *, direct=True, version=0):
        catalog = await asyncio.to_thread(self.source.client.catalog, "supplier-far")
        return Advertisement(request_id=uuid.uuid4().hex, expected_version=version,
            observed_at=self.now, origin=self.source.origin if direct else None, catalog=catalog)

    async def forward_one(self, session):
        until = asyncio.get_running_loop().time() + 8
        while asyncio.get_running_loop().time() < until:
            poll = await self.http.post(f"/v1/instances/{session.instance_id}/relay/poll",
                json={"session_id": session.session_id, "epoch": session.epoch}, headers=self.auth())
            self.assertEqual(poll.status_code, 200, poll.text)
            delivery = PollResult.model_validate(poll.json()).delivery
            if delivery is None:
                continue
            arguments = {"session_id": session.session_id, "epoch": session.epoch, "delivery_id": delivery.delivery_id}
            try:
                reply = await asyncio.to_thread(self.source.client.forward, delivery.request)
            except ServiceError as error:
                arguments["error"] = {"status": error.status, "code": error.code}
            else:
                arguments["response"] = reply
            complete = await self.http.post(f"/v1/instances/{session.instance_id}/relay/complete",
                json=CompleteRelay(**arguments).model_dump(mode="json"), headers=self.auth())
            self.assertEqual(complete.status_code, 200, complete.text)
            return
        self.fail("relay fixture did not receive a request")

    async def test_verified_direct_advertisement_is_searchable_and_withdrawal_is_versioned(self):
        ad = await self.advertisement()
        url = f"/v1/instances/{self.first}/publications/supplier-far"
        response = await self.http.put(url, json=ad.model_dump(mode="json"), headers=self.auth())
        self.assertEqual(response.status_code, 200, response.text)
        search = SupplierSearch(profile=term("material-supply"), inputs=query(revision=ad.catalog.revision).inputs)
        result = await self.http.post("/v1/suppliers/search", json=search.model_dump(mode="json"))
        self.assertEqual(result.status_code, 200, result.text)
        candidate = result.json()["candidates"][0]
        self.assertEqual(candidate["instance_id"], self.first)
        self.assertEqual(candidate["direct_origin"], self.source.origin)
        self.assertNotIn("fields", candidate["listing"])
        removed = await self.http.post(url + "/withdraw", headers=self.auth(), json={"request_id": uuid.uuid4().hex, "expected_version": 1})
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertFalse((await self.http.post("/v1/suppliers/search", json=search.model_dump(mode="json"))).json()["candidates"])
        stale = await self.http.put(url, json=ad.model_dump(mode="json"), headers=self.auth())
        self.assertEqual(stale.status_code, 409)

    async def test_ownership_and_audience_are_checked_before_parsing_untrusted_bodies(self):
        url = f"/v1/instances/{self.first}/publications/supplier-far"
        for headers, expected in (({}, 401), (self.auth("owner-b"), 403),
                                   (self.auth("unbound"), 403),
                                   (self.auth(changes={"aud": "https://another.example/mcp"}), 401)):
            response = await self.http.put(url, content=b"not JSON", headers={"Content-Type": "application/json", **headers})
            self.assertEqual(response.status_code, expected, response.text)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM publications").fetchone()[0], 0)

    async def test_relay_only_supplier_can_publish_and_answer_the_same_query(self):
        opened = await self.http.post(f"/v1/instances/{self.first}/relay/open", headers=self.auth())
        self.assertEqual(opened.status_code, 200, opened.text)
        session = RelaySession.model_validate(opened.json())
        ad = await self.advertisement(direct=False)
        forwarding = asyncio.create_task(self.forward_one(session))
        saved = await self.http.put(f"/v1/instances/{self.first}/publications/supplier-far", json=ad.model_dump(mode="json"), headers=self.auth())
        await forwarding
        self.assertEqual(saved.status_code, 200, saved.text)
        envelope = PeerRequest(instance_id=self.first, request_id=uuid.uuid4().hex,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=8), query=PeerListing(
                shop_id="supplier-far", listing_id="service", query=query(revision=ad.catalog.revision), expected_generation=1))
        forwarding = asyncio.create_task(self.forward_one(session))
        response = await self.http.post("/v1/relay/query", json=envelope.model_dump(mode="json"))
        await forwarding
        self.assertEqual(response.status_code, 200, response.text)
        direct = await asyncio.to_thread(self.source.client.forward, envelope)
        self.assertEqual(PeerResponse.model_validate(response.json()), direct)
        self.source.service.withdraw(self.source.owner, "supplier-far", expected_generation=1)
        envelope = envelope.model_copy(update={"request_id": uuid.uuid4().hex})
        forwarding = asyncio.create_task(self.forward_one(session))
        missing = await self.http.post("/v1/relay/query", json=envelope.model_dump(mode="json"))
        await forwarding
        self.assertEqual(missing.status_code, 404, missing.text)

    async def test_lost_advertisement_reply_can_be_read_without_extending_a_stale_lease(self):
        ad = await self.advertisement()
        url = f"/v1/instances/{self.first}/publications/supplier-far"
        saved = await self.http.put(url, json=ad.model_dump(mode="json"), headers=self.auth())
        self.assertEqual(saved.status_code, 200, saved.text)
        self.source.service.withdraw(self.source.owner, "supplier-far", expected_generation=1)
        self.now += timedelta(seconds=121)
        repeated = await self.http.put(url, json=ad.model_dump(mode="json"), headers=self.auth())
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json(), saved.json())
        page = await self.http.post("/v1/suppliers/search", json={"profile": term("material-supply").model_dump(mode="json")})
        self.assertFalse(page.json()["candidates"])

    async def test_source_catalog_mismatch_and_unapproved_origins_do_not_change_the_index(self):
        ad = await self.advertisement()
        url = f"/v1/instances/{self.first}/publications/supplier-far"
        changed = ad.model_copy(update={"catalog": ad.catalog.model_copy(update={"generation": 2})})
        response = await self.http.put(url, json=changed.model_dump(mode="json"), headers=self.auth())
        self.assertEqual(response.status_code, 502, response.text)
        blocked = ad.model_copy(update={"origin": "https://127.0.0.1:8443"})
        response = await self.http.put(url, json=blocked.model_dump(mode="json"), headers=self.auth())
        self.assertEqual(response.status_code, 502, response.text)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM listings").fetchone()[0], 0)

    async def test_offline_relay_and_private_session_access_fail_explicitly(self):
        envelope = PeerRequest(instance_id=self.first, request_id=uuid.uuid4().hex,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=5), query=PeerCatalog(shop_id="supplier-far"))
        response = await self.http.post("/v1/relay/query", json=envelope.model_dump(mode="json"))
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "relay_offline")
        response = await self.http.post(f"/v1/instances/{self.first}/relay/open", headers=self.auth("owner-b"))
        self.assertEqual(response.status_code, 403)

    async def test_missing_request_accounting_cannot_be_silently_reinitialized(self):
        limits = self.settings.identity.database.with_name(self.settings.identity.database.name + ".limits")
        limits.unlink()
        with self.assertRaises(ValueError):
            create_cloud_app(self.settings, store=self.store, verifier=self.identity.verifier)

    async def replace_settings(self, settings):
        await self.http.aclose()
        await self.lifespan.__aexit__(None, None, None)
        self.settings = settings
        self.store = DirectoryStore(settings, clock=lambda: self.now)
        self.app = create_cloud_app(settings, store=self.store, verifier=self.identity.verifier)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url="https://cloud.example")

    async def test_publish_and_connect_grants_are_independent_and_revocation_hides_existing_ads(self):
        ad = await self.advertisement()
        settings = self.settings.model_copy(update={"bindings": (
            OwnerBinding(subject="owner-a", instance_id=self.first, connect=False),
        )})
        await self.replace_settings(settings)
        opened = await self.http.post(f"/v1/instances/{self.first}/relay/open", headers=self.auth())
        self.assertEqual(opened.status_code, 403)
        published = await self.http.put(f"/v1/instances/{self.first}/publications/supplier-far",
            json=ad.model_dump(mode="json"), headers=self.auth())
        self.assertEqual(published.status_code, 200, published.text)
        await self.replace_settings(settings.model_copy(update={"bindings": (
            OwnerBinding(subject="owner-a", instance_id=self.first, advertise=False),
        )}))
        opened = await self.http.post(f"/v1/instances/{self.first}/relay/open", headers=self.auth())
        self.assertEqual(opened.status_code, 200, opened.text)
        denied = await self.http.get(f"/v1/instances/{self.first}/publications/supplier-far", headers=self.auth())
        self.assertEqual(denied.status_code, 403)
        searched = await self.http.post("/v1/suppliers/search", json={"profile": term("material-supply").model_dump(mode="json")})
        self.assertFalse(searched.json()["candidates"])

    async def test_second_serving_process_cannot_share_live_session_state(self):
        other = create_cloud_app(self.settings, store=self.store, verifier=self.identity.verifier)
        with self.assertRaisesRegex(ValueError, "already being served"):
            async with other.router.lifespan_context(other):
                self.fail("a second cloud server acquired the same state")
