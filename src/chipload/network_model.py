"""Caller-owned public network reads; credentials and egress policy are not job inputs."""

from typing import Annotated, Literal

from pydantic import Field, StrictInt, StrictStr

from .artifact import canonical_json, digest
from .directory_model import SearchPage, SupplierSearch
from .federation_wire import Origin
from .model import Id, Model, Timestamp
from .peer_model import InstanceId, PeerRequest, PeerResponse, RequestId

MAX_NETWORK_REQUEST = 128 * 1024
MAX_NETWORK_RESULT = 3 * 1024**2 - 128 * 1024
NETWORK_RETENTION_SECONDS = 3600


class DirectRead(Model):
    kind: Literal["direct"] = "direct"
    origin: Origin
    request: PeerRequest


class RelayRead(Model):
    kind: Literal["relay"] = "relay"
    cloud: Id
    request: PeerRequest


class DirectoryRead(Model):
    kind: Literal["search"] = "search"
    cloud: Id
    directory_id: InstanceId
    query: SupplierSearch


NetworkQuery = Annotated[DirectRead | RelayRead | DirectoryRead, Field(discriminator="kind")]


class NetworkSubmit(Model):
    shop_id: Id
    request_key: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$", max_length=100)]
    deadline: Timestamp
    query: NetworkQuery


class NetworkRead(Model):
    shop_id: Id
    job_id: RequestId


class NetworkClaim(Model):
    shop_id: Id


class NetworkLease(Model):
    shop_id: Id
    job_id: RequestId
    claim: RequestId
    request: NetworkSubmit


class NetworkError(Model):
    code: Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$", max_length=64)]
    status: Annotated[StrictInt, Field(ge=400, le=599)]


class NetworkComplete(NetworkRead):
    claim: RequestId
    result: PeerResponse | SearchPage | None = None
    error: NetworkError | None = None


class NetworkRecord(Model):
    id: RequestId
    shop_id: Id
    state: Literal["queued", "running", "succeeded", "failed", "cancelled", "expired"]
    created_at: Timestamp
    deadline: Timestamp
    finished_at: Timestamp | None
    result: PeerResponse | SearchPage | None = None
    error: NetworkError | None = None


def search_hash(query):
    return digest(canonical_json(query.model_dump(mode="json", exclude={"cursor"})))


NETWORK_SCHEMA = """
CREATE TABLE IF NOT EXISTS network_jobs(
 id TEXT PRIMARY KEY, shop_id TEXT NOT NULL REFERENCES shops(id), actor TEXT NOT NULL,
 request_key TEXT NOT NULL, intent BLOB NOT NULL, state TEXT NOT NULL,
 created REAL NOT NULL, deadline REAL NOT NULL, finished REAL,
 claim TEXT, dispatcher TEXT, result BLOB, error BLOB,
 UNIQUE(shop_id,actor,request_key));
CREATE INDEX IF NOT EXISTS network_pending ON network_jobs(state,created,id);
CREATE INDEX IF NOT EXISTS network_owners ON network_jobs(actor,shop_id,state);
"""
