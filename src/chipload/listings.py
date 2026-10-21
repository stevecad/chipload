"""Exact semantic input translation for published supplier listings."""

from typing import Literal

from pydantic import Field, StrictBool, model_validator

from .artifact import Digest
from .evaluate import EvaluationRequest
from .model import Assurance, Fact, Model, SemanticRef, Value


class ListingInput(Model):
    term: SemanticRef
    value: SemanticRef | Value


class ListingQuery(Model):
    profile: SemanticRef
    expected_revision: Digest
    inputs: tuple[ListingInput, ...] = Field(default=(), max_length=64)
    purpose: Literal["capability", "readiness"] = "capability"
    minimum_evidence: Assurance = "declared"
    allow_simulated: StrictBool = False

    @model_validator(mode="after")
    def unique_inputs(self):
        if len({item.term for item in self.inputs}) != len(self.inputs):
            raise ValueError("listing query input terms must be unique")
        return self


def translate(listing, query: ListingQuery) -> EvaluationRequest | None:
    """None means an unknown semantic binding, never a rejected physical process.

    Do not discard unknown requirements or treat raw strings as vocabulary aliases.
    Missing mapped inputs remain absent for the normal evaluator to classify.
    """
    if query.profile != listing.profile:
        return None
    fields = {field.term: field for field in listing.fields}
    values = {"facts": [], "initial_state": []}
    for item in query.inputs:
        field = fields.get(item.term)
        if field is None:
            return None
        value = item.value
        if field.choices:
            choice = next((choice for choice in field.choices if choice.term == value), None)
            if choice is None:
                return None
            value = choice.value
        elif isinstance(value, SemanticRef):
            return None
        values[field.context].append(Fact(name=field.fact, value=value))
    return EvaluationRequest(
        target_kind=listing.target_kind, target=listing.target,
        facts=tuple(values["facts"]), initial_state=tuple(values["initial_state"]),
        purpose=query.purpose, minimum_evidence=query.minimum_evidence,
        allow_simulated=query.allow_simulated,
    )
