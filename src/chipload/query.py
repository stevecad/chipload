"""Contextual shop-variable queries over data; no shop source is executed."""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .artifact import Artifact
from .discovery import public_catalog
from .evaluate import check_rule
from .model import Fact, Id, Model, Timestamp, VariableValue, aware


class VariableQuery(Model):
    ids: Annotated[tuple[Id, ...], Field(max_length=100)] = ()
    category: Id | None = None
    context: Annotated[tuple[Fact, ...], Field(max_length=100)] = ()

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len(set(self.ids)) != len(self.ids):
            raise ValueError("variable identifiers must be unique")
        if len({fact.name for fact in self.context}) != len(self.context):
            raise ValueError("context fact names must be unique")
        return self


class ApplicabilityReason(Model):
    code: Id
    fact: Id | None = None
    message: str


class VariableFinding(Model):
    definition: dict
    applicability: Literal["applicable", "not-applicable", "unknown"]
    effective_value: VariableValue
    reasons: tuple[ApplicabilityReason, ...] = ()


class VariableResult(Model):
    revision: str
    evaluated_at: Timestamp
    variables: tuple[VariableFinding, ...]
    unused_context: tuple[Id, ...] = ()


def query_variables(
    artifact: Artifact, query: VariableQuery, *, at: datetime, public_only: bool = True
) -> VariableResult:
    """Resolve applicability without interpreting a description as a hard rule.

    Private mode is for authorized service callers only. Public mode filters
    before lookup and never evaluates a hidden catalog predicate as an oracle.
    """
    aware(at)
    if public_only:
        catalog = public_catalog(artifact)
        views = {variable["id"]: variable for variable in catalog["variables"]}
        revision = catalog["public_sha256"]
    else:
        views = {
            v.id: v.model_dump(mode="json", exclude={"visibility"}) for v in artifact.shop.variables
        }
        revision = artifact.model_sha256
    if any(id not in views for id in query.ids):
        raise KeyError("unknown variable")
    selected = [
        v
        for v in artifact.shop.variables
        if v.id in views
        and (not query.ids or v.id in query.ids)
        and (query.category is None or v.category == query.category)
    ]
    facts = {fact.name: fact.value for fact in query.context}
    used = set()
    findings = []
    for variable in selected:
        reasons = []
        states = []
        for rule, view in zip(
            variable.applies_when, views[variable.id]["applies_when"], strict=True
        ):
            used.add(rule.fact)
            if view.get("complete") is False:
                states.append("unknown")
                reasons.append(
                    ApplicabilityReason(
                        code="unpublished-condition",
                        fact=rule.fact,
                        message="This condition is not fully published.",
                    )
                )
                continue
            failure = check_rule(rule, facts, ("variables", variable.id, "applies_when"))
            if failure:
                states.append("not-applicable" if failure.outcome == "unsupported" else "unknown")
                reasons.append(
                    ApplicabilityReason(code=failure.code, fact=rule.fact, message=failure.message)
                )
        applicable = (
            "not-applicable"
            if "not-applicable" in states
            else ("unknown" if "unknown" in states else "applicable")
        )
        if variable.recorded_at > at or (
            variable.valid_until is not None and at >= variable.valid_until
        ):
            applicable = "unknown"
            reasons.append(
                ApplicabilityReason(
                    code="statement-not-current",
                    message="This statement is not current at this time.",
                )
            )
        if variable.certainty == "unknown":
            reasons.append(
                ApplicabilityReason(
                    code="unknown-value", message="The shop has not supplied this value."
                )
            )
        findings.append(
            VariableFinding(
                definition=views[variable.id],
                applicability=applicable,
                effective_value=variable.value if applicable == "applicable" else None,
                reasons=tuple(reasons),
            )
        )
    return VariableResult(
        revision=revision,
        evaluated_at=at,
        variables=tuple(findings),
        unused_context=tuple(sorted(facts.keys() - used)),
    )
