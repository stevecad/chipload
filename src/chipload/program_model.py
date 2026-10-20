"""Bounded Python runs. A run is an automation attempt, not a shop publication."""

from typing import Annotated, Literal

from pydantic import Field, JsonValue, StrictInt, StrictStr, model_validator

from .artifact import Digest, canonical_json
from .model import Id, Model, Timestamp
from .project import SourceFile

MAX_PROGRAM_BYTES = 256 * 1024
MAX_PROGRAM_RESULT = 256 * 1024
MAX_PROGRAM_LOG = 64 * 1024
MAX_CALLS = 64
PROGRAM_RESERVATION = MAX_PROGRAM_BYTES + MAX_PROGRAM_RESULT + 6 * MAX_PROGRAM_LOG + 192 * 1024


class RunPython(Model):
    shop_id: Id
    request_key: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")]
    code: Annotated[StrictStr, Field(min_length=1, max_length=64 * 1024)]
    files: tuple[SourceFile, ...] = Field(default=(), max_length=32)
    inputs: dict[str, JsonValue] = Field(default_factory=dict, max_length=100)
    seconds: Annotated[StrictInt, Field(ge=1, le=30)] = 30

    @model_validator(mode="after")
    def bounded_program(self):
        # Reuse project path collision rules without importing or executing source.
        from .project import SourceProject

        SourceProject(
            shop_id=self.shop_id,
            entrypoint="main:run",
            files=(SourceFile(path="main.py", content=self.code), *self.files),
        )
        if len(canonical_json(self)) > MAX_PROGRAM_BYTES:
            raise ValueError("Python run exceeds its byte limit")
        return self


class ReadRun(Model):
    shop_id: Id
    run_id: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{32}$")]


class CallRecord(Model):
    operation: Annotated[StrictStr, Field(pattern=r"^[a-z_]{1,64}$")]
    request_sha256: Digest
    state: Literal["started", "returned", "error"]
    read_only: bool
    status: Annotated[StrictInt, Field(ge=400, le=599)] | None = None
    references: dict[str, Annotated[StrictStr, Field(max_length=100)] | StrictInt] = Field(
        default_factory=dict, max_length=5
    )


class RunRecord(Model):
    id: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{32}$")]
    shop_id: Id
    actor: str
    request_sha256: Digest
    state: Literal["queued", "running", "succeeded", "failed", "cancelled", "interrupted"]
    created_at: Timestamp
    deadline: Timestamp | None
    finished_at: Timestamp | None
    cancel_requested: bool
    # Script output is private, user-authored data, never an authoritative build report.
    value: JsonValue = None
    logs: Annotated[StrictStr, Field(max_length=MAX_PROGRAM_LOG)] = ""
    error: Annotated[StrictStr, Field(max_length=1000)] | None = None
    calls: tuple[CallRecord, ...] = Field(default=(), max_length=MAX_CALLS)

    @model_validator(mode="after")
    def bounded_result(self):
        if len(canonical_json(self.value)) > MAX_PROGRAM_RESULT:
            raise ValueError("Python result exceeds its byte limit")
        return self


PROGRAM_SCHEMA = """
CREATE TABLE python_runs (
    id TEXT PRIMARY KEY, shop_id TEXT NOT NULL REFERENCES shops(id), actor TEXT NOT NULL,
    request_key TEXT NOT NULL, request BLOB NOT NULL, request_sha256 TEXT NOT NULL,
    ceiling BLOB NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','cancelled','interrupted')),
    created_at TEXT NOT NULL, deadline TEXT, finished_at TEXT, claim TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    result BLOB, calls BLOB NOT NULL, scan_turn INTEGER NOT NULL DEFAULT 0,
    UNIQUE(shop_id,actor,request_key)
);
CREATE INDEX python_runs_queue ON python_runs(state,scan_turn,created_at,id);
"""
