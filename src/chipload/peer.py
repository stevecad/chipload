"""Direct public instance client; the relay uses the same request/reply models."""

import uuid
from datetime import datetime, timedelta, timezone

from pydantic import TypeAdapter

from .artifact import canonical_json
from .client import TransportError
from .federation_wire import EgressPolicy, checked_origin, request
from .peer_model import (
    MAX_PEER_REQUEST_BYTES,
    InstanceId,
    PeerCatalog,
    PeerDescriptor,
    PeerListing,
    PeerQuery,
    PeerRequest,
    PeerResponse,
)


class PeerClient:
    def __init__(self, origin, instance_id, *, policy: EgressPolicy | None = None):
        self.origin = checked_origin(origin)
        self.instance_id = TypeAdapter(InstanceId).validate_python(instance_id)
        self.policy = policy or EgressPolicy()

    def descriptor(self):
        value = request(self.origin, "GET", "/.well-known/chipload", policy=self.policy)
        try:
            result = PeerDescriptor.model_validate(value)
            if result.instance_id != self.instance_id:
                raise ValueError("instance mismatch")
            return result
        except ValueError:
            raise TransportError("Peer descriptor identity or contract does not match") from None

    def query(self, query):
        query = TypeAdapter(PeerQuery).validate_python(query)
        envelope = PeerRequest(instance_id=self.instance_id, request_id=uuid.uuid4().hex,
                               deadline=datetime.now(timezone.utc) + timedelta(seconds=self.policy.seconds),
                               query=query)
        if len(canonical_json(envelope)) > MAX_PEER_REQUEST_BYTES:
            raise ValueError("peer request byte limit")
        value = request(self.origin, "POST", "/v1/peer/query", policy=self.policy,
                        body=envelope.model_dump(mode="json"))
        try:
            result = PeerResponse.model_validate(value)
            if (result.instance_id != envelope.instance_id or result.request_id != envelope.request_id
                    or result.result.action != query.action
                    or datetime.now(timezone.utc) >= envelope.deadline):
                raise ValueError("mismatched or late peer reply")
            if query.expected_generation is not None and result.result.generation != query.expected_generation:
                raise ValueError("mismatched peer generation")
            expected_revision = (query.query.expected_revision if query.action == "listing_evaluate"
                                 else getattr(query, "expected_revision", None))
            if expected_revision is not None and result.result.revision != expected_revision:
                raise ValueError("mismatched peer revision")
            if query.action == "catalog" and result.result.value["shop"]["id"] != query.shop_id:
                raise ValueError("mismatched peer shop")
            if query.action == "listing_evaluate" and result.result.value["listing_id"] != query.listing_id:
                raise ValueError("mismatched peer listing")
            if query.action == "evaluate" and any(
                result.result.value[name] != getattr(query.query, name) for name in ("target_kind", "target")
            ):
                raise ValueError("mismatched evaluation target")
            return result.result
        except ValueError:
            raise TransportError("Peer response identity or contract does not match") from None

    def catalog(self, shop_id, *, expected_generation=None):
        return self.query(PeerCatalog(shop_id=shop_id, expected_generation=expected_generation))

    def evaluate_listing(self, shop_id, listing_id, query, *, expected_generation=None):
        return self.query(PeerListing(shop_id=shop_id, listing_id=listing_id, query=query,
                                      expected_generation=expected_generation))
