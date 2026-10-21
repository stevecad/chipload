"""Conventional Python/connector client for the optional cloud directory and relay."""

import uuid
from datetime import datetime, timedelta, timezone

from pydantic import TypeAdapter

from .artifact import canonical_json, digest
from .client import TransportError
from .directory_model import (
    Advertisement,
    DirectoryDescriptor,
    PublicationReceipt,
    SearchPage,
    SupplierSearch,
    Withdrawal,
)
from .federation_wire import EgressPolicy, checked_origin, request
from .model import Id
from .peer import checked_reply
from .peer_model import InstanceId, PeerCatalog, PeerListing, PeerQuery, PeerRequest
from .relay import CompleteRelay, PollResult, RelaySession


class CloudClient:
    def __init__(self, origin, directory_id, *, policy=None, token=None):
        self.origin, self.directory_id = checked_origin(origin), TypeAdapter(InstanceId).validate_python(directory_id)
        self.policy, self.token = policy or EgressPolicy(), token

    def call(self, method, path, body=None, *, private=False):
        token = (self.token() if callable(self.token) else self.token) if private else None
        if private and not token:
            raise ValueError("cloud enrollment credentials are required")
        return request(self.origin, method, path, policy=self.policy,
                       body=body.model_dump(mode="json") if hasattr(body, "model_dump") else body, token=token)

    @staticmethod
    def path(instance):
        return "/v1/instances/" + TypeAdapter(InstanceId).validate_python(instance)

    @staticmethod
    def session_body(session):
        return {"session_id": session.session_id, "epoch": session.epoch}

    @staticmethod
    def parse(schema, value):
        try:
            return schema.model_validate(value)
        except ValueError:
            raise TransportError("Cloud response contract does not match") from None

    def descriptor(self):
        result = self.parse(DirectoryDescriptor, self.call("GET", "/.well-known/chipload-cloud"))
        if result.directory_id != self.directory_id:
            raise TransportError("Cloud directory identity does not match")
        return result

    def search(self, query):
        query = SupplierSearch.model_validate(query)
        result = self.parse(SearchPage, self.call("POST", "/v1/suppliers/search", query))
        expected = digest(canonical_json(query.model_dump(mode="json", exclude={"cursor"})))
        if (result.directory_id != self.directory_id or result.query_sha256 != expected
                or len(result.candidates) > query.limit
                or any(item.listing.profile != query.profile for item in result.candidates)):
            raise TransportError("Cloud search identity or query does not match")
        now = datetime.now(timezone.utc)
        age = (now - result.evaluated_at).total_seconds()
        if not -5 <= age <= self.policy.seconds + 5 or any(item.expires_at <= now for item in result.candidates):
            raise TransportError("Cloud search response is stale")
        return result

    def receipt(self, value, instance, shop, *, change=None):
        result = self.parse(PublicationReceipt, value)
        if (result.instance_id != instance or result.shop_id != shop or (change is not None and (
            result.request_id != change.request_id or result.version != change.expected_version + 1
        ))):
            raise TransportError("Cloud publication receipt does not match")
        return result

    def head(self, instance, shop):
        return self.receipt(self.call("GET", self.path(instance) + "/publications/" + TypeAdapter(Id).validate_python(shop), private=True), instance, shop)

    def advertise(self, instance, shop, advertisement):
        advertisement = Advertisement.model_validate(advertisement)
        result = self.receipt(self.call("PUT", self.path(instance) + "/publications/" + TypeAdapter(Id).validate_python(shop), advertisement, private=True), instance, shop, change=advertisement)
        if result.source_generation != advertisement.catalog.generation or result.source_revision != advertisement.catalog.revision:
            raise TransportError("Cloud receipt references another source publication")
        return result

    def withdraw(self, instance, shop, withdrawal):
        withdrawal = Withdrawal.model_validate(withdrawal)
        return self.receipt(self.call("POST", self.path(instance) + "/publications/" + TypeAdapter(Id).validate_python(shop) + "/withdraw", withdrawal, private=True), instance, shop, change=withdrawal)

    def open(self, instance):
        result = self.parse(RelaySession, self.call("POST", self.path(instance) + "/relay/open", private=True))
        if result.instance_id != instance:
            raise TransportError("Cloud relay instance does not match")
        return result

    def poll(self, session):
        if self.policy.seconds < 4:
            raise ValueError("relay polling requires a transport budget of at least four seconds")
        body = {**self.session_body(session), "wait_seconds": min(10, self.policy.seconds - 3)}
        result = self.parse(PollResult, self.call("POST", self.path(session.instance_id) + "/relay/poll", body, private=True))
        if result.delivery is not None and result.delivery.request.instance_id != session.instance_id:
            raise TransportError("Cloud delivery belongs to another instance")
        return result

    def complete(self, session, delivery_id, *, response=None, error=None):
        body = CompleteRelay(**self.session_body(session), delivery_id=delivery_id, response=response, error=error)
        return self.call("POST", self.path(session.instance_id) + "/relay/complete", body, private=True)

    def close(self, session):
        return self.call("POST", self.path(session.instance_id) + "/relay/close", self.session_body(session), private=True)

    def forward(self, envelope):
        envelope = PeerRequest.model_validate(envelope)
        value = self.call("POST", "/v1/relay/query", envelope)
        return checked_reply(envelope, value)

    def query(self, instance, query):
        envelope = PeerRequest(instance_id=instance, request_id=uuid.uuid4().hex,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=self.policy.seconds),
            query=TypeAdapter(PeerQuery).validate_python(query))
        return self.forward(envelope).result

    def catalog(self, instance, shop, *, expected_generation=None):
        return self.query(instance, PeerCatalog(shop_id=shop, expected_generation=expected_generation))

    def evaluate_listing(self, instance, shop, listing, query, *, expected_generation=None):
        return self.query(instance, PeerListing(shop_id=shop, listing_id=listing, query=query, expected_generation=expected_generation))
