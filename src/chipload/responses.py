"""Explicit public response envelopes; internal evaluation objects stay private."""

from typing import Literal

from pydantic import Field

from .artifact import Digest
from .evaluate import Outcome
from .model import Id, Model, Route, Source, Timestamp


class CatalogView(Model):
    schema_version: str
    public_sha256: Digest
    shop: dict
    sites: tuple[dict, ...]
    resources: tuple[dict, ...]
    catalogs: tuple[dict, ...]
    processes: tuple[dict, ...]
    capabilities: tuple[dict, ...]
    workflows: tuple[dict, ...]
    variables: tuple[dict, ...]
    operations: tuple[dict, ...] = ()
    offers: tuple[dict, ...] = Field(default=(), exclude_if=lambda value: not value)


class PageView(Model):
    revision: Digest
    items: tuple[dict, ...]
    next_cursor: str | None


class ItemView(Model):
    revision: Digest
    item: dict


class PublicReason(Model):
    code: Id
    message: str


class Freshness(Model):
    state: Literal["fresh", "missing", "stale", "future", "mismatched", "simulated", "unknown"]
    time: Timestamp | None
    expires_at: Timestamp | None
    source: Source | None


class PublicAlternative(Route):
    outcome: Outcome
    simulated: bool


class PublicEvaluation(Model):
    target_kind: Literal["capability", "workflow", "offer"]
    target: Id
    revision: Digest
    evaluated_at: Timestamp
    outcome: Outcome
    simulated: bool
    reasons: tuple[PublicReason, ...]
    freshness: tuple[Freshness, ...]
    alternatives: tuple[PublicAlternative, ...] = Field(default=(), max_length=32, exclude_if=lambda value: not value)
    matching_alternative: Route | None = Field(default=None, exclude_if=lambda value: value is None)
