"""Bounded read-only relay queues; enrolled instances poll over outbound HTTPS."""

import asyncio
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Annotated

from pydantic import Field, StrictInt, StrictStr, model_validator

from .artifact import canonical_json
from .client import TransportError
from .lifecycle import Conflict, NotFound
from .model import Model, Timestamp
from .peer import checked_reply
from .peer_model import MAX_PEER_REQUEST_BYTES, InstanceId, PeerRequest, PeerResponse, RequestId


class RelayFailure(RuntimeError):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__("Relay query could not complete")


class RelaySession(Model):
    instance_id: InstanceId
    session_id: RequestId
    epoch: Annotated[StrictInt, Field(ge=1, le=2**63 - 1)]
    expires_at: Timestamp


class SessionCommand(Model):
    session_id: RequestId
    epoch: Annotated[StrictInt, Field(ge=1, le=2**63 - 1)]


class RelayPoll(SessionCommand):
    wait_seconds: Annotated[StrictInt, Field(ge=1, le=10)] = 5


class RelayDelivery(Model):
    delivery_id: RequestId
    request: PeerRequest


class PollResult(Model):
    delivery: RelayDelivery | None


class DestinationError(Model):
    status: Annotated[StrictInt, Field(ge=400, le=599)]
    code: Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$", max_length=64)]


class CompleteRelay(SessionCommand):
    delivery_id: RequestId
    response: PeerResponse | None = None
    error: DestinationError | None = None

    @model_validator(mode="after")
    def one_reply(self):
        if (self.response is None) == (self.error is None):
            raise ValueError("complete requires exactly one response or destination error")
        return self


@dataclass
class Session:
    id: str
    epoch: int
    until: float
    queue: deque = field(default_factory=deque)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    polling: bool = False


@dataclass
class Pending:
    instance: str
    session: str
    requester: str
    request: PeerRequest
    future: asyncio.Future
    delivered: bool = False


class RelayBroker:
    """One event loop/process. Restart drops read requests; clients see failure."""

    def __init__(self, *, max_pending=64, per_instance=8, per_requester=4, session_seconds=45, poll_seconds=10):
        self.sessions, self.pending = {}, {}
        self.requesters, self.destinations = Counter(), Counter()
        self.max_pending, self.per_instance, self.per_requester = max_pending, per_instance, per_requester
        self.session_seconds, self.poll_seconds = session_seconds, poll_seconds

    def connected(self):
        now = time.monotonic()
        return frozenset(key for key, session in self.sessions.items() if session.until > now)

    def open(self, instance, epoch):
        old = self.sessions.get(instance)
        if old and old.epoch >= epoch:
            raise Conflict("a newer relay session already exists")
        if old:
            self.fail_session(instance, old)
        if not old and len(self.sessions) >= 1024:
            for key, session in list(self.sessions.items()):
                if session.until <= time.monotonic():
                    self.fail_session(key, session)
                    del self.sessions[key]
            if len(self.sessions) >= 1024:
                raise RelayFailure(503, "relay_capacity")
        session = Session(uuid.uuid4().hex, epoch, time.monotonic() + self.session_seconds)
        self.sessions[instance] = session
        return RelaySession(instance_id=instance, session_id=session.id, epoch=epoch,
                            expires_at=datetime.now(timezone.utc) + timedelta(seconds=self.session_seconds))

    def fail_session(self, instance, session):
        session.until = 0
        session.queue.clear()
        session.wake.set()
        for pending in self.pending.values():
            if pending.instance == instance and pending.session == session.id and not pending.future.done():
                pending.future.set_exception(RelayFailure(503, "relay_session_replaced"))

    def session(self, instance, command):
        session = self.sessions.get(instance)
        if (session is None or session.id != command.session_id or session.epoch != command.epoch
                or session.until <= time.monotonic()):
            raise Conflict("relay session is absent, expired or replaced")
        return session

    def close(self, instance, command):
        session = self.session(instance, command)
        self.fail_session(instance, session)
        del self.sessions[instance]

    async def poll(self, instance, command, *, wait_seconds=None):
        session = self.session(instance, command)
        if session.polling:
            raise Conflict("only one poll may be active for a relay session")
        session.polling = True
        session.until = time.monotonic() + self.session_seconds
        until = time.monotonic() + (min(self.poll_seconds, wait_seconds) if wait_seconds is not None else self.poll_seconds)
        try:
            while True:
                self.session(instance, command)
                while session.queue:
                    identifier = session.queue.popleft()
                    pending = self.pending.get(identifier)
                    if pending is not None and not pending.future.done():
                        pending.delivered = True
                        session.until = time.monotonic() + self.session_seconds
                        return PollResult(delivery=RelayDelivery(delivery_id=identifier, request=pending.request))
                remaining = until - time.monotonic()
                if remaining <= 0:
                    session.until = time.monotonic() + self.session_seconds
                    return PollResult(delivery=None)
                session.wake.clear()
                try:
                    await asyncio.wait_for(session.wake.wait(), remaining)
                except TimeoutError:
                    pass
        finally:
            session.polling = False

    async def submit(self, request, requester):
        request = PeerRequest.model_validate(request)
        remaining = (request.deadline - datetime.now(timezone.utc)).total_seconds()
        if not 0 < remaining <= 30 or len(canonical_json(request)) > MAX_PEER_REQUEST_BYTES:
            raise ValueError("relay request exceeds its deadline or byte limit")
        session = self.sessions.get(request.instance_id)
        if session is None or session.until <= time.monotonic():
            raise RelayFailure(503, "relay_offline")
        if (len(self.pending) >= self.max_pending or self.destinations[request.instance_id] >= self.per_instance
                or self.requesters[requester] >= self.per_requester):
            raise RelayFailure(503, "relay_capacity")
        if any(item.instance == request.instance_id and item.request.request_id == request.request_id for item in self.pending.values()):
            raise Conflict("request ID already has a pending relay delivery")
        identifier = uuid.uuid4().hex
        pending = Pending(request.instance_id, session.id, requester, request, asyncio.get_running_loop().create_future())
        self.pending[identifier] = pending
        self.requesters[requester] += 1
        self.destinations[request.instance_id] += 1
        session.queue.append(identifier)
        session.wake.set()
        try:
            return await asyncio.wait_for(asyncio.shield(pending.future), remaining)
        except TimeoutError:
            raise RelayFailure(504, "relay_deadline") from None
        finally:
            self.pending.pop(identifier, None)
            for counter, key in ((self.requesters, requester), (self.destinations, request.instance_id)):
                counter[key] -= 1
                if not counter[key]:
                    del counter[key]
            if identifier in session.queue:
                session.queue.remove(identifier)
            if not pending.future.done():
                pending.future.cancel()
            elif not pending.future.cancelled():
                pending.future.exception()  # Retrieve a failure even if the HTTP caller disconnected.

    def complete(self, instance, command):
        command = CompleteRelay.model_validate(command)
        session = self.session(instance, command)
        pending = self.pending.get(command.delivery_id)
        if (pending is None or pending.instance != instance or pending.session != session.id
                or not pending.delivered or pending.future.done()):
            raise NotFound("relay delivery is no longer active")
        if datetime.now(timezone.utc) >= pending.request.deadline:
            raise Conflict("relay delivery deadline expired")
        if command.error is not None:
            pending.future.set_exception(RelayFailure(command.error.status, "relay_destination"))
        else:
            try:
                checked_reply(pending.request, command.response.model_dump(mode="json"))
            except TransportError:
                raise ValueError("relay response does not match its delivery") from None
            pending.future.set_result(command.response)
        session.until = time.monotonic() + self.session_seconds
