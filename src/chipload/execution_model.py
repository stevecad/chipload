"""Bounded execution requests, adapter outcomes, and durable storage definitions."""

from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from .artifact import Digest
from .model import Fact, Id, Model, ShopOperation, Timestamp

MAX_EXECUTION_BYTES = 64 * 1024
# Reserve both the execution result and the simulated receiver's durable receipt.
RESULT_RESERVATION_BYTES = 2 * MAX_EXECUTION_BYTES
ExecutionMode = Literal["simulated", "live"]
ExecutionState = Literal["queued", "running", "succeeded", "rejected", "cancelled", "indeterminate"]
RequestKey = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")]


class ExecutionRequest(Model):
    shop_id: Id
    operation_id: Id
    expected_generation: Annotated[StrictInt, Field(ge=1)]
    request_key: RequestKey
    mode: ExecutionMode
    inputs: Annotated[tuple[Fact, ...], Field(max_length=100)] = ()

    @model_validator(mode="after")
    def unique_inputs(self):
        if len({f.name for f in self.inputs}) != len(self.inputs):
            raise ValueError("operation input names must be unique")
        return self


class AdapterBinding(Model):
    shop_id: Id
    operation_id: Id
    model_sha256: Digest
    adapter_id: Id
    mode: ExecutionMode
    adapter_sha256: Digest | None = Field(default=None, exclude_if=lambda value: value is None)
    enabled: StrictBool = True


class OperationInspection(Model):
    generation: int
    model_sha256: Digest
    operation: ShopOperation
    binding_generation: int
    enabled: StrictBool
    mode: ExecutionMode | None


class AdapterResult(Model):
    # Rejected/cancelled assert that no effect occurred and no late effect can occur.
    # Failure after a possibly accepted command must remain indeterminate.
    state: Literal["succeeded", "rejected", "cancelled", "indeterminate"]
    code: Id
    outputs: Annotated[tuple[Fact, ...], Field(max_length=100)] = ()

    @model_validator(mode="after")
    def successful_outputs(self):
        if self.state != "succeeded" and self.outputs:
            raise ValueError("only successful executions have confirmed outputs")
        if len({f.name for f in self.outputs}) != len(self.outputs):
            raise ValueError("operation output names must be unique")
        return self


class ExecutionRecord(Model):
    id: Digest
    shop_id: Id
    operation_id: Id
    actor: Annotated[StrictStr, Field(max_length=200)]
    mode: ExecutionMode
    publication_generation: int
    model_sha256: Digest
    binding_generation: int
    state: ExecutionState
    created_at: Timestamp
    deadline: Timestamp | None
    finished_at: Timestamp | None
    cancel_requested: StrictBool
    result: AdapterResult | None


EXECUTION_SCHEMA = """
CREATE TABLE operation_bindings (
    shop_id TEXT NOT NULL REFERENCES shops(id), operation_id TEXT NOT NULL,
    generation INTEGER NOT NULL, binding BLOB NOT NULL,
    PRIMARY KEY(shop_id, operation_id)
);
CREATE TABLE executions (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
    shop_id TEXT NOT NULL REFERENCES shops(id), operation_id TEXT NOT NULL,
    actor TEXT NOT NULL, request_key TEXT NOT NULL, request_sha256 TEXT NOT NULL,
    request BLOB NOT NULL, build_id TEXT NOT NULL REFERENCES builds(id),
    model_sha256 TEXT NOT NULL, publication_generation INTEGER NOT NULL,
    binding_generation INTEGER NOT NULL, binding BLOB NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','rejected','cancelled','indeterminate')),
    created_at TEXT NOT NULL, deadline TEXT, finished_at TEXT, claim TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0, scan_turn INTEGER NOT NULL DEFAULT 0, result BLOB,
    UNIQUE(shop_id, actor, request_key)
);
CREATE INDEX executions_by_shop ON executions(shop_id, state, sequence);
CREATE INDEX executions_by_actor ON executions(actor, state, sequence);
CREATE INDEX executions_by_state ON executions(state, deadline, sequence);
CREATE INDEX executions_by_scan ON executions(state, scan_turn, sequence);
CREATE TABLE execution_resources (
    shop_id TEXT NOT NULL, resource_id TEXT NOT NULL,
    execution_id TEXT NOT NULL REFERENCES executions(id),
    PRIMARY KEY(shop_id, resource_id)
);
CREATE TABLE execution_turns (
    scope TEXT NOT NULL, subject TEXT NOT NULL, turn INTEGER NOT NULL,
    PRIMARY KEY(scope,subject)
);
CREATE TABLE simulation_effects (
    execution_id TEXT PRIMARY KEY REFERENCES executions(id),
    request_sha256 TEXT NOT NULL, result BLOB NOT NULL
);
"""
