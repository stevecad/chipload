"""Trusted outbound publication and public-query connector; no shop code executes here."""

import argparse
import logging
import re
import signal
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from pydantic import Field, StrictBool, StrictInt, model_validator

from .artifact import canonical_json
from .client import ServiceError, TransportError
from .cloud_client import CloudClient
from .connector_auth import CredentialUnavailable, WorkloadIdentity, WorkloadToken
from .directory_model import Advertisement, Withdrawal
from .federation_wire import EgressPolicy, Origin
from .limits import bounded_json
from .model import Id, Model
from .peer import PeerClient
from .peer_model import InstanceId
from .remote_adapter import private_bytes

LOGGER = logging.getLogger("chipload.connector")


class CloudLink(Model):
    id: Id
    origin: Origin
    directory_id: InstanceId
    credentials: Path
    identity: WorkloadIdentity
    advertise: StrictBool = True
    relay: StrictBool = True

    @model_validator(mode="after")
    def scope(self):
        if self.identity.trust.resource_url != self.origin + "/mcp":
            raise ValueError("cloud credential audience must identify the configured cloud origin")
        if not self.credentials.is_absolute():
            raise ValueError("cloud credentials require an absolute directory")
        if self.identity.policy.seconds < 4:
            raise ValueError("cloud transport requires at least four seconds")
        if not self.advertise and not self.relay:
            raise ValueError("a cloud link must enable publication or relay")
        return self


class ConnectorSettings(Model):
    instance_id: InstanceId
    source_origin: Origin
    source_policy: EgressPolicy = EgressPolicy()
    public_origin: Origin | None = None
    shops: tuple[Id, ...] = Field(default=(), max_length=64)
    clouds: tuple[CloudLink, ...] = Field(min_length=1, max_length=4)
    renew_seconds: Annotated[StrictInt, Field(ge=10, le=60)] = 30
    lease_seconds: Annotated[StrictInt, Field(ge=30, le=300)] = 120

    @model_validator(mode="after")
    def unique_links(self):
        if len(set(self.shops)) != len(self.shops):
            raise ValueError("connector shop IDs must be unique")
        for key in ("id", "origin", "directory_id"):
            if len({getattr(link, key) for link in self.clouds}) != len(self.clouds):
                raise ValueError("cloud links must have unique identities and origins")
        if self.lease_seconds < 3 * self.renew_seconds:
            raise ValueError("publication leases must cover at least three renewal intervals")
        if any(link.advertise for link in self.clouds) and not self.shops:
            raise ValueError("publication requires an explicit shop list")
        if self.public_origin is None and any(link.advertise and not link.relay for link in self.clouds):
            raise ValueError("publication requires a direct origin or outbound relay")
        return self


class Connector:
    def __init__(self, settings: ConnectorSettings, *, token_factory=None):
        self.settings = settings
        self.source = PeerClient(settings.source_origin, settings.instance_id, policy=settings.source_policy)
        self.stop = threading.Event()
        self.slots = threading.BoundedSemaphore(4)
        self.workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="peer-read")
        self.threads, self.links, self.sessions, self.ready = [], {}, {}, {}
        self._started, self._closed = False, False
        try:
            for link in settings.clouds:
                provider = (token_factory(link) if token_factory else WorkloadToken(link.identity, link.credentials))
                client = CloudClient(link.origin, link.directory_id, policy=link.identity.policy, token=provider)
                self.links[link.id] = (link, client, provider)
                self.ready[link.id] = threading.Event()
        except BaseException:
            self.close()
            raise

    def start(self):
        if self._started or self._closed:
            raise ValueError("connector can be started only once")
        self.source.descriptor()
        # Verify cloud identities before sending any enrollment token.
        for _, client, _ in self.links.values():
            client.descriptor()
        self._started = True
        for link, client, _ in self.links.values():
            for target in ((self.relay_loop,) if link.relay else ()) + ((self.publication_loop,) if link.advertise else ()):
                thread = threading.Thread(target=target, args=(link, client), name=f"connector-{link.id}-{target.__name__}")
                self.threads.append(thread)
                thread.start()

    @staticmethod
    def invalidate(provider, error):
        if isinstance(error, ServiceError) and error.status == 401 and hasattr(provider, "invalidate"):
            provider.invalidate()

    def relay_loop(self, link, client):
        session, delay = None, 1
        provider = self.links[link.id][2]
        while not self.stop.is_set():
            acquired = False
            try:
                if session is None:
                    client.descriptor()
                    session = client.open(self.settings.instance_id)
                    self.sessions[link.id] = session
                    self.ready[link.id].set()
                acquired = self.slots.acquire(timeout=.5)
                if not acquired:
                    continue
                delivery = client.poll(session).delivery
                if delivery is None:
                    self.slots.release()
                    acquired = False
                else:
                    future = self.workers.submit(self.forward, client, session, delivery)
                    acquired = False
                    future.add_done_callback(self.release)
                delay = 1
            except (ServiceError, TransportError, CredentialUnavailable, ValueError) as error:
                if acquired:
                    self.slots.release()
                self.invalidate(provider, error)
                session = None
                self.ready[link.id].clear()
                LOGGER.warning("relay_unavailable cloud=%s", link.id)
                if self.stop.wait(delay):
                    break
                delay = min(30, delay * 2)
        if session is not None and self.sessions.pop(link.id, None) == session:
            self.close_session(link.id, client, session)

    @staticmethod
    def close_session(link_id, client, session):
        try:
            client.close(session)
        except ServiceError as error:
            if error.status != 409:
                LOGGER.warning("relay_close_unconfirmed cloud=%s", link_id)
        except (TransportError, CredentialUnavailable, ValueError):
            LOGGER.warning("relay_close_unconfirmed cloud=%s", link_id)

    def release(self, future):
        self.slots.release()
        if future.exception() is not None:
            LOGGER.warning("relay_delivery_unconfirmed")

    def forward(self, client, session, delivery):
        try:
            response = self.source.forward(delivery.request)
            # Leave room for the completion envelope, token and operator policy.
            if len(canonical_json(response)) > 3 * 1024**2 - 64 * 1024:
                return client.complete(session, delivery.delivery_id,
                    error={"status": 502, "code": "peer_response_too_large"})
        except ServiceError as error:
            code = error.code if isinstance(error.code, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", error.code) else "peer_refused"
            return client.complete(session, delivery.delivery_id, error={"status": error.status, "code": code})
        except (TransportError, ValueError):
            return client.complete(session, delivery.delivery_id, error={"status": 502, "code": "peer_unavailable"})
        return client.complete(session, delivery.delivery_id, response=response)

    def refresh(self, link_id):
        link, client, provider = self.links[link_id]
        if not link.advertise:
            raise ValueError("cloud publication is disabled")
        for shop in self.settings.shops:
            if self.stop.is_set():
                return
            try:
                try:
                    catalog = self.source.catalog(shop)
                    observed = datetime.now(timezone.utc)
                    indexed = any(item.get("indexed") for item in catalog.value.get("listings", ()))
                except ServiceError as error:
                    if error.status != 404:
                        raise
                    catalog, indexed = None, False
                try:
                    head = client.head(self.settings.instance_id, shop)
                except ServiceError as error:
                    if error.status != 404:
                        raise
                    head = None
                if not indexed:
                    if head is not None and head.expires_at is not None:
                        client.withdraw(self.settings.instance_id, shop,
                            Withdrawal(request_id=uuid.uuid4().hex, expected_version=head.version))
                    continue
                client.advertise(self.settings.instance_id, shop, Advertisement(
                    request_id=uuid.uuid4().hex, expected_version=head.version if head else 0,
                    observed_at=observed, lease_seconds=self.settings.lease_seconds,
                    origin=self.settings.public_origin, catalog=catalog))
            except (ServiceError, TransportError, CredentialUnavailable, ValueError) as error:
                self.invalidate(provider, error)
                LOGGER.warning("publication_refresh_failed cloud=%s shop=%s", link_id, shop)

    def publication_loop(self, link, client):
        while not self.stop.is_set():
            if self.settings.public_origin is None and not self.ready[link.id].wait(.5):
                continue
            self.refresh(link.id)
            self.stop.wait(self.settings.renew_seconds)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.stop.set()
        # Closing a known session wakes its idle poll. Pending public reads fail;
        # no old request is replayed when the connector starts again.
        for link_id, session in tuple(self.sessions.items()):
            self.sessions.pop(link_id, None)
            self.close_session(link_id, self.links[link_id][1], session)
        for thread in self.threads:
            thread.join()
        self.workers.shutdown(wait=True, cancel_futures=False)
        for _, _, provider in self.links.values():
            if hasattr(provider, "close"):
                provider.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    settings = ConnectorSettings.model_validate(bounded_json(private_bytes(args.config, 1024**2), max_bytes=1024**2))
    connector = Connector(settings)
    for name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(name, lambda *_: connector.stop.set())
    try:
        connector.start()
        connector.stop.wait()
    finally:
        connector.close()


if __name__ == "__main__":
    main()
