"""Cloud enrollment, publication leases and exact supplier search contracts."""

from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator

from .artifact import Digest
from .auth import ServerSettings
from .federation_wire import EgressPolicy, Origin
from .listings import ListingInput
from .model import GeoPoint, Id, Model, Name, SemanticRef, Text, Timestamp
from .peer_model import InstanceId, PeerResult, RequestId

MAX_ADVERTISEMENT_BYTES = 2 * 1024**2


class OwnerBinding(Model):
    subject: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    instance_id: InstanceId
    advertise: StrictBool = True
    connect: StrictBool = True


class CloudSettings(Model):
    directory_id: InstanceId
    identity: ServerSettings
    bindings: tuple[OwnerBinding, ...] = Field(max_length=1024)
    egress: EgressPolicy = EgressPolicy()
    max_publications: Annotated[StrictInt, Field(ge=1, le=4096)] = 256
    max_listings: Annotated[StrictInt, Field(ge=1, le=16384)] = 4096
    max_fields: Annotated[StrictInt, Field(ge=1, le=131072)] = 16384
    max_choices: Annotated[StrictInt, Field(ge=1, le=262144)] = 32768

    @model_validator(mode="after")
    def unique_owners(self):
        if len({b.instance_id for b in self.bindings}) != len(self.bindings):
            raise ValueError("each enrolled instance must have one owner binding")
        if not self.identity.database.is_absolute():
            raise ValueError("cloud database path must be absolute")
        if self.identity.bindings or self.identity.instance_id is not None:
            raise ValueError("cloud ownership uses OwnerBinding and directory_id, not shop grants or an API instance ID")
        return self


class Advertisement(Model):
    request_id: RequestId
    expected_version: Annotated[StrictInt, Field(ge=0)]
    observed_at: Timestamp
    lease_seconds: Annotated[StrictInt, Field(ge=30, le=300)] = 120
    origin: Origin | None = None
    catalog: PeerResult

    @model_validator(mode="after")
    def catalog_only(self):
        if self.catalog.action != "catalog":
            raise ValueError("an advertisement requires a public catalog snapshot")
        return self


class Withdrawal(Model):
    request_id: RequestId
    expected_version: Annotated[StrictInt, Field(ge=1)]


class PublicationReceipt(Model):
    instance_id: InstanceId
    shop_id: Id
    version: Annotated[StrictInt, Field(ge=1)]
    request_id: RequestId
    source_generation: Annotated[StrictInt, Field(ge=1)]
    source_revision: Digest
    expires_at: Timestamp | None
    indexed_listings: Annotated[StrictInt, Field(ge=0)]


class SupplierSearch(Model):
    profile: SemanticRef
    inputs: tuple[ListingInput, ...] = Field(default=(), max_length=64)
    origin: GeoPoint | None = None
    radius_m: Annotated[StrictInt, Field(ge=1, le=20_100_000)] | None = None
    max_accuracy_m: Annotated[StrictInt, Field(ge=0, le=1_000_000)] = 1_000_000
    include_unlocated: StrictBool = False
    limit: Annotated[StrictInt, Field(ge=1, le=100)] = 20
    cursor: Annotated[StrictStr, Field(max_length=2048)] | None = None

    @model_validator(mode="after")
    def meaningful_search(self):
        if self.radius_m is not None and self.origin is None:
            raise ValueError("a radius requires a search origin")
        if len({item.term for item in self.inputs}) != len(self.inputs):
            raise ValueError("search input terms must be unique")
        return self


class SupplierCandidate(Model):
    instance_id: InstanceId
    shop_id: Id
    shop_name: Annotated[StrictStr, Field(max_length=200)]
    listing: "ListingSummary"
    source_generation: Annotated[StrictInt, Field(ge=1)]
    source_revision: Digest
    expires_at: Timestamp
    direct_origin: Origin | None
    relay_available: StrictBool
    location: GeoPoint | None
    distance_m: Annotated[StrictFloat, Field(ge=0, le=20_100_000, allow_inf_nan=False)] | None
    distance_uncertainty_m: Annotated[StrictInt, Field(ge=0)] | None


class SearchPage(Model):
    directory_id: InstanceId
    snapshot: Digest
    query_sha256: Digest
    evaluated_at: Timestamp
    candidates: tuple[SupplierCandidate, ...] = Field(max_length=100)
    next_cursor: Annotated[StrictStr, Field(max_length=2048)] | None
    scope: Annotated[StrictStr, Field(max_length=400)] = "This directory's live advertisements; reported straight-line site distances. Candidates require fresh supplier evaluation."


class ListingSummary(Model):
    id: Id
    name: Name
    description: Text = ""
    profile: SemanticRef
    site: Id | None
    target_kind: Literal["capability", "workflow", "offer"]
    target: Id


class DirectoryDescriptor(Model):
    protocol: Literal["chipload-cloud-1"] = "chipload-cloud-1"
    directory_id: InstanceId


SupplierCandidate.model_rebuild()
