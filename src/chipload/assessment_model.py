"""Typed on-demand queries and private queue bindings; never executes shop code."""

from typing import Annotated, Literal

from pydantic import Field, JsonValue, StrictBool, StrictInt, StrictStr

from .artifact import Digest
from .evaluate import EvaluationRequest
from .model import Id, Model, PythonCheck, Timestamp
from .project import SourceProject


class AssessShop(Model):
    shop_id: Id
    request_key: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")]
    query: EvaluationRequest
    expected_revision: Digest
    private: StrictBool = False
    seconds: Annotated[StrictInt, Field(ge=1, le=30)] = 30


class AssessmentTask(AssessShop):
    # Only the trusted service creates these bindings. Clients submit AssessShop.
    build_id: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{32}$")]
    build_sha256: Digest
    generation: Annotated[StrictInt, Field(ge=1)]


class AssessmentRead(Model):
    shop_id: Id
    assessment_id: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{32}$")]


class CheckCall(Model):
    capability: Id
    check: PythonCheck


class AssessmentWork(Model):
    project: SourceProject
    query: EvaluationRequest
    model_sha256: Digest
    checks: tuple[CheckCall, ...] = Field(max_length=128)


class AssessmentRecord(Model):
    id: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{32}$")]
    shop_id: Id
    state: Literal["queued", "running", "succeeded", "failed", "cancelled", "interrupted", "superseded"]
    created_at: Timestamp
    finished_at: Timestamp | None
    cancel_requested: bool
    # An as-of receipt. New live queries require another request key.
    evaluation: dict[str, JsonValue] | None = None
    error: Literal["Assessment failed", "Assessment interrupted", "Assessment superseded"] | None = None


ASSESSMENT_MIGRATION = """
ALTER TABLE python_runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'program'
    CHECK(kind IN ('program','assessment'));
"""
