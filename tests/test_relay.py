"""Relay session fencing, independent delivery identity and bounded pending work."""

import asyncio
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from chipload import BuildIdentity, compile_shop
from chipload.discovery import public_catalog
from chipload.lifecycle import Conflict, NotFound
from chipload.peer_model import PeerCatalog, PeerRequest, PeerResponse, PeerResult
from chipload.relay import CompleteRelay, RelayBroker, RelayFailure, SessionCommand
from examples.federation_shops import define_shop


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.broker = RelayBroker(poll_seconds=.05)
        self.instance = "a" * 32
        self.session = self.broker.open(self.instance, 1)
        self.command = SessionCommand(session_id=self.session.session_id, epoch=1)
        artifact = compile_shop(define_shop(), BuildIdentity(source_sha256="a" * 64,
            sdk_sha256="b" * 64, dependencies_sha256="c" * 64, runtime="relay-test"))
        catalog = public_catalog(artifact)
        self.result = PeerResult(action="catalog", generation=1, revision=catalog["public_sha256"], value=catalog)

    def request(self, *, seconds=2, identifier=None):
        return PeerRequest(instance_id=self.instance, request_id=identifier or uuid.uuid4().hex,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=seconds),
            query=PeerCatalog(shop_id="supplier-far", expected_generation=1))

    def complete(self, delivery, **changes):
        return CompleteRelay(**self.command.model_dump(), delivery_id=delivery.delivery_id,
            response=PeerResponse(instance_id=self.instance, request_id=delivery.request.request_id,
                                  result=self.result), **changes)

    async def submit(self, request=None):
        task = asyncio.create_task(self.broker.submit(request or self.request(), "requester"))
        await asyncio.sleep(0)
        return task

    async def test_public_reply_keeps_destination_request_and_publication(self):
        task = await self.submit()
        delivery = (await self.broker.poll(self.instance, self.command)).delivery
        reply = self.complete(delivery)
        self.broker.complete(self.instance, reply)
        self.assertEqual(await task, reply.response)
        self.assertFalse(self.broker.pending)
        self.assertFalse(self.broker.requesters)

    async def test_session_replacement_fences_old_poll_and_pending_work(self):
        task = await self.submit()
        self.broker.open(self.instance, 2)
        with self.assertRaises(RelayFailure) as replaced:
            await task
        self.assertEqual(replaced.exception.code, "relay_session_replaced")
        with self.assertRaises(Conflict):
            await self.broker.poll(self.instance, self.command)
        with self.assertRaises(Conflict):
            self.broker.open(self.instance, 1)

    async def test_per_requester_and_destination_bounds_release_after_cancellation(self):
        self.broker.per_requester = 1
        task = await self.submit()
        with self.assertRaises(RelayFailure) as busy:
            await self.broker.submit(self.request(), "requester")
        self.assertEqual(busy.exception.code, "relay_capacity")
        self.broker.per_requester = 4
        self.broker.per_instance = 1
        with self.assertRaises(RelayFailure):
            await self.broker.submit(self.request(), "another")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.broker.pending)
        self.assertFalse(self.broker.sessions[self.instance].queue)

    async def test_deadline_removes_work_and_late_delivery_cannot_complete_reused_request_id(self):
        identifier = uuid.uuid4().hex
        task = await self.submit(self.request(seconds=.08, identifier=identifier))
        old = (await self.broker.poll(self.instance, self.command)).delivery
        with self.assertRaises(RelayFailure) as timed_out:
            await task
        self.assertEqual(timed_out.exception.status, 504)
        task = await self.submit(self.request(identifier=identifier))
        new = (await self.broker.poll(self.instance, self.command)).delivery
        self.assertNotEqual(old.delivery_id, new.delivery_id)
        with self.assertRaises(NotFound):
            self.broker.complete(self.instance, self.complete(old))
        self.broker.complete(self.instance, self.complete(new))
        await task

    async def test_another_enrolled_instance_cannot_complete_a_delivery(self):
        task = await self.submit()
        delivery = (await self.broker.poll(self.instance, self.command)).delivery
        second = self.broker.open("b" * 32, 1)
        reply = self.complete(delivery).model_copy(update={"session_id": second.session_id})
        with self.assertRaises(NotFound):
            self.broker.complete("b" * 32, reply)
        self.broker.complete(self.instance, self.complete(delivery))
        await task

    async def test_wrong_response_does_not_settle_the_public_request(self):
        task = await self.submit()
        delivery = (await self.broker.poll(self.instance, self.command)).delivery
        reply = self.complete(delivery)
        invalid = reply.model_copy(update={"response": reply.response.model_copy(update={"request_id": "0" * 32})})
        with self.assertRaises(ValueError):
            self.broker.complete(self.instance, invalid)
        self.assertFalse(task.done())
        self.broker.complete(self.instance, reply)
        await task

    async def test_only_one_poll_and_no_offline_queue(self):
        waiting = asyncio.create_task(self.broker.poll(self.instance, self.command))
        await asyncio.sleep(0)
        with self.assertRaises(Conflict):
            await self.broker.poll(self.instance, self.command)
        self.assertIsNone((await waiting).delivery)
        self.broker.close(self.instance, self.command)
        with self.assertRaises(RelayFailure) as offline:
            await self.broker.submit(self.request(), "requester")
        self.assertEqual(offline.exception.code, "relay_offline")

    async def test_destination_failure_is_a_transport_refusal_not_an_unsupported_process(self):
        task = await self.submit()
        delivery = (await self.broker.poll(self.instance, self.command)).delivery
        self.broker.complete(self.instance, CompleteRelay(**self.command.model_dump(),
            delivery_id=delivery.delivery_id, error={"status": 404, "code": "not_found"}))
        with self.assertRaises(RelayFailure) as refused:
            await task
        self.assertEqual(refused.exception.status, 404)
