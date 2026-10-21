"""Public instance protocol shared by direct and relayed requests."""

from typing import Annotated, Literal

from pydantic import Field, StrictInt, StrictStr, model_validator

from .artifact import Digest, canonical_json, digest
from .evaluate import EvaluationRequest
from .listings import ListingQuery
from .model import Id, Model, Timestamp
from .query import VariableQuery, VariableResult
from .responses import CatalogView, ListingEvaluation, PublicEvaluation

InstanceId = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{32}$", max_length=32)]
RequestId = InstanceId
MAX_PEER_REQUEST_BYTES = 128 * 1024


class PeerCatalog(Model):
    action: Literal["catalog"] = "catalog"
    shop_id: Id
    expected_generation: Annotated[StrictInt, Field(ge=1)] | None = None


class PeerListing(Model):
    action: Literal["listing_evaluate"] = "listing_evaluate"
    shop_id: Id
    listing_id: Id
    query: ListingQuery
    expected_generation: Annotated[StrictInt, Field(ge=1)] | None = None


class PeerEvaluate(Model):
    action: Literal["evaluate"] = "evaluate"
    shop_id: Id
    expected_revision: Digest
    query: EvaluationRequest
    expected_generation: Annotated[StrictInt, Field(ge=1)] | None = None


class PeerVariables(Model):
    action: Literal["variables"] = "variables"
    shop_id: Id
    expected_revision: Digest
    query: VariableQuery
    expected_generation: Annotated[StrictInt, Field(ge=1)] | None = None


PeerQuery = Annotated[PeerCatalog | PeerListing | PeerEvaluate | PeerVariables, Field(discriminator="action")]


class PeerRequest(Model):
    protocol: Literal["chipload-peer-1"] = "chipload-peer-1"
    instance_id: InstanceId
    request_id: RequestId
    deadline: Timestamp
    query: PeerQuery


class PeerResult(Model):
    action: Literal["catalog", "listing_evaluate", "evaluate", "variables"]
    generation: Annotated[StrictInt, Field(ge=1)]
    revision: Digest
    value: dict

    @model_validator(mode="after")
    def typed_value(self):
        schema = {"catalog": CatalogView, "listing_evaluate": ListingEvaluation,
                  "evaluate": PublicEvaluation, "variables": VariableResult}[self.action]
        value = schema.model_validate(self.value).model_dump(mode="json")
        revision = value["public_sha256"] if self.action == "catalog" else value["revision"]
        if revision != self.revision:
            raise ValueError("peer result and value revisions must agree")
        if self.action == "catalog" and digest(canonical_json(
            {key: item for key, item in self.value.items() if key != "public_sha256"}
        )) != revision:
            raise ValueError("peer catalog digest does not match its content")
        return self


class PeerResponse(Model):
    protocol: Literal["chipload-peer-1"] = "chipload-peer-1"
    instance_id: InstanceId
    request_id: RequestId
    result: PeerResult


class PeerDescriptor(Model):
    protocol: Literal["chipload-peer-1"] = "chipload-peer-1"
    instance_id: InstanceId
    actions: tuple[Literal["catalog", "listing_evaluate", "evaluate", "variables"], ...] = (
        "catalog", "listing_evaluate", "evaluate", "variables",
    )
