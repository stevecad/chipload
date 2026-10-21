"""Deterministic capability evaluation over compiled data.

No source imports, network calls, equipment actions, or LLM interpretation occur
here. These results are internal domain data; transports must enforce visibility.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, model_validator

from .artifact import Artifact
from .graph import ordered_nodes
from .model import (
    Assurance,
    Capability,
    Choice,
    Fact,
    Id,
    Model,
    Observation,
    Quantity,
    Range,
    Route,
    Rule,
    Source,
    Timestamp,
    Value,
    aware,
)
from .python_checks import PythonEvaluation, validated_outcomes

Outcome = Literal["supported", "unsupported", "conditional", "unknown"]
RANK = {"supported": 0, "conditional": 1, "unknown": 2, "unsupported": 3}
ASSURANCE = {"declared": 0, "demonstrated": 1, "qualified": 2}


class EvaluationRequest(Model):
    target_kind: Literal["capability", "workflow", "offer"]
    target: Id
    facts: Annotated[tuple[Fact, ...], Field(max_length=100)] = ()
    initial_state: Annotated[tuple[Fact, ...], Field(max_length=100)] = ()
    purpose: Literal["capability", "readiness"] = "capability"
    minimum_evidence: Assurance = "declared"
    allow_simulated: StrictBool = False

    @model_validator(mode="after")
    def unique_facts(self) -> Self:
        for values in (self.facts, self.initial_state):
            if len({f.name for f in values}) != len(values):
                raise ValueError("fact names must be unique within a collection")
        return self


class Reason(Model):
    code: Id
    outcome: Outcome
    path: tuple[str, ...]
    message: str


class EvidenceUse(Model):
    id: Id
    level: Assurance
    source: Source
    recorded_at: Timestamp
    valid_until: Timestamp | None


class ObservationUse(Model):
    id: Id
    state: Literal["fresh", "missing", "stale", "future", "mismatched", "simulated", "unknown"]
    time: Timestamp | None = None
    expires_at: Timestamp | None = None
    source: Source | None = None


class Decision(Model):
    outcome: Outcome
    reasons: tuple[Reason, ...]
    assumptions: tuple[Fact, ...] = ()
    evidence: tuple[EvidenceUse, ...] = ()
    observations: tuple[ObservationUse, ...] = ()


class StepResult(Model):
    step: Id
    capability: Id
    decision: Decision


class AlternativeResult(Route):
    decision: Decision
    steps: tuple[StepResult, ...] = ()
    simulated: StrictBool


class Evaluation(Decision):
    target_kind: Literal["capability", "workflow", "offer"]
    target: Id
    model_sha256: str
    evaluated_at: Timestamp
    simulated: StrictBool
    steps: tuple[StepResult, ...] = ()
    alternatives: tuple[AlternativeResult, ...] = Field(
        default=(), max_length=32, exclude_if=lambda value: not value
    )
    matching_alternative: Route | None = Field(default=None, exclude_if=lambda value: value is None)


def same(left: Value, right: Value) -> bool:
    if isinstance(left, Quantity) and isinstance(right, Quantity):
        return left.dimension == right.dimension and left.base_value() == right.base_value()
    return type(left) is type(right) and left == right


def outcome(reasons: list[Reason] | tuple[Reason, ...]) -> Outcome:
    return max((r.outcome for r in reasons), key=RANK.__getitem__, default="supported")


def check_rule(rule: Rule, facts: dict[str, Value], path: tuple[str, ...]) -> Reason | None:
    value = facts.get(rule.fact)
    if value is None:
        return Reason(
            code="missing-fact",
            outcome=rule.on_missing,
            path=path + (rule.fact,),
            message=rule.remedy or f"Supply {rule.fact} to evaluate this requirement.",
        )
    if isinstance(rule, Range):
        bound = rule.minimum if rule.minimum is not None else rule.maximum
        assert bound is not None
        if not isinstance(value, Quantity) or value.dimension != bound.dimension:
            return Reason(
                code="incompatible-units",
                outcome="unknown",
                path=path + (rule.fact,),
                message=f"{rule.fact} requires a {bound.dimension} measurement.",
            )
        within = (rule.minimum is None or value.base_value() >= rule.minimum.base_value()) and (
            rule.maximum is None or value.base_value() <= rule.maximum.base_value()
        )
        if not within:
            return Reason(
                code="outside-range",
                outcome=rule.on_mismatch,
                path=path + (rule.fact,),
                message=rule.remedy or f"{rule.fact} is outside this declared envelope.",
            )
        if rule.multiple_of is not None:
            if (value.base_value() / rule.multiple_of.base_value()).denominator != 1:
                return Reason(
                    code="invalid-increment",
                    outcome=rule.on_mismatch,
                    path=path + (rule.fact,),
                    message=rule.remedy or f"{rule.fact} does not meet the required increment.",
                )
    elif not any(same(value, candidate) for candidate in rule.allowed):
        return Reason(
            code="value-not-supported",
            outcome=rule.on_mismatch,
            path=path + (rule.fact,),
            message=rule.remedy or f"{rule.fact} is not an accepted value.",
        )
    return None


def evaluate(
    artifact: Artifact,
    request: EvaluationRequest,
    *,
    at: datetime,
    observations: tuple[Observation, ...] = (),
    revoked_evidence: frozenset[str] = frozenset(),
    python_evaluation: PythonEvaluation | None = None,
) -> Evaluation:
    """Evaluate a validated artifact with no caller-supplied knowledge exemptions."""
    return _evaluate(
        artifact, request, at=at, observations=observations, revoked_evidence=revoked_evidence,
        python_results=validated_outcomes(artifact, request, at, python_evaluation),
    )


def _evaluate(
    artifact: Artifact,
    request: EvaluationRequest,
    *,
    at: datetime,
    observations: tuple[Observation, ...] = (),
    declared_facts: frozenset[str] = frozenset(),
    revoked_evidence: frozenset[str] = frozenset(),
    python_results: dict | None = None,
) -> Evaluation:
    """Evaluate an already validated artifact at an explicit observation time.

    Serving layers load artifacts using read_artifact and authorize both the
    target and returned data. `at` comes from the service clock for live queries.
    """
    aware(at)
    if request.target_kind == "offer":
        from .offers import evaluate_offer

        return evaluate_offer(
            artifact, request, at=at, observations=observations, revoked_evidence=revoked_evidence,
            python_results=python_results,
        )
    facts = {fact.name: fact.value for fact in request.facts}
    initial_state = {fact.name: fact.value for fact in request.initial_state}
    observation_index = {item.id: item for item in observations}
    if len(observation_index) != len(observations):
        raise ValueError("ambiguous observation snapshot: duplicate identifiers")
    capabilities = {c.id: c for c in artifact.shop.capabilities}
    evidence_index = {e.id: e for e in artifact.shop.evidence}
    resources = {r.id: r for r in artifact.shop.resources}

    def decide(capability: Capability, state: dict[str, Value]) -> Decision:
        path = ("capabilities", capability.id)
        reasons: list[Reason] = []
        assumptions: list[Fact] = []
        effective_facts = facts.copy()
        for check in capability.python_checks:
            result = (python_results or {}).get((capability.id, check.id))
            reasons.append(Reason(
                code="python-check-" + (result or "required"),
                outcome=result or "unknown",
                path=path + ("python_checks", check.id),
                message=check.remedy if result == "conditional" else {
                    None: "This request requires an isolated Python check.",
                    "supported": "The Python check accepts these inputs.",
                    "unsupported": "The Python check rejects these inputs.",
                    "unknown": "The Python check cannot establish support for these inputs.",
                }.get(result, ""),
            ))
        for rule in capability.requirements:
            result = check_rule(rule, facts, path + ("requirements",))
            if result is not None:
                reasons.append(result)
                # A single declared remedy can define a planned setup. Preserve it
                # as an explicit assumption; never silently turn it into support.
                if result.outcome == "conditional" and isinstance(rule, Choice):
                    if len(rule.allowed) == 1:
                        assumption = Fact(name=rule.fact, value=rule.allowed[0])
                        assumptions.append(assumption)
                        effective_facts[rule.fact] = assumption.value
        for fact in capability.input_state:
            value = state.get(fact.name)
            if value is None:
                reasons.append(
                    Reason(
                        code="missing-input-state",
                        outcome="unknown",
                        path=path + ("input_state", fact.name),
                        message=f"The incoming {fact.name} is unknown.",
                    )
                )
            elif not same(value, fact.value):
                reasons.append(
                    Reason(
                        code="incompatible-input-state",
                        outcome="unsupported",
                        path=path + ("input_state", fact.name),
                        message=f"The incoming {fact.name} is incompatible.",
                    )
                )
        for resource_id in capability.resources:
            if resources[resource_id].status == "retired":
                reasons.append(
                    Reason(
                        code="resource-retired",
                        outcome="unsupported",
                        path=path + ("resources", resource_id),
                        message="This offer depends on a retired resource; revise the offer and evidence.",
                    )
                )
            elif resources[resource_id].status != "active":
                reasons.append(
                    Reason(
                        code="resource-unavailable",
                        outcome="conditional",
                        path=path + ("resources", resource_id),
                        message="An associated resource must be restored or replaced.",
                    )
                )

        uses: list[EvidenceUse] = []
        required_facts = {rule.fact for rule in capability.requirements}
        for evidence_id in capability.evidence:
            if evidence_id in revoked_evidence:
                continue
            proof = evidence_index[evidence_id]
            proof_rules = {rule.fact: rule for rule in proof.requirements}
            catalog_mismatch = any(
                not isinstance(proof_rules.get(rule.fact), Choice)
                or proof_rules[rule.fact].catalog != rule.catalog
                for rule in capability.requirements
                if isinstance(rule, Choice) and rule.catalog is not None
            )
            if (
                ASSURANCE[proof.level] < ASSURANCE[request.minimum_evidence]
                or proof.recorded_at > at
                or (proof.valid_until is not None and at >= proof.valid_until)
                or (proof.source == "simulated" and not request.allow_simulated)
                or not set(capability.resources).issubset(proof.resources)
                or not required_facts.issubset({r.fact for r in proof.requirements})
                or catalog_mismatch
                or any(check_rule(r, effective_facts, ()) is not None for r in proof.requirements)
            ):
                continue
            uses.append(
                EvidenceUse(
                    id=proof.id,
                    level=proof.level,
                    source=proof.source,
                    recorded_at=proof.recorded_at,
                    valid_until=proof.valid_until,
                )
            )
        if not uses:
            reasons.append(
                Reason(
                    code="evidence-unavailable",
                    outcome="unknown",
                    path=path + ("evidence",),
                    message="No current evidence covers these inputs, resources, and assurance level.",
                )
            )

        observation_uses: list[ObservationUse] = []
        for requirement in capability.observations:
            if requirement.purpose == "readiness" and request.purpose != "readiness":
                continue
            observed = observation_index.get(requirement.observation)
            observation_state = "fresh"
            if observed is None:
                observation_state = "missing"
            elif (observed.resource, observed.property) != (
                requirement.resource,
                requirement.property,
            ):
                observation_state = "mismatched"
            elif observed.time > at:
                observation_state = "future"
            elif at >= observed.expires_at:
                observation_state = "stale"
            elif observed.source == "simulated" and not request.allow_simulated:
                observation_state = "simulated"
            elif observed.value is None:
                observation_state = "unknown"
            observation_uses.append(
                ObservationUse(
                    id=requirement.observation,
                    state=observation_state,
                    time=observed.time if observed else None,
                    expires_at=observed.expires_at if observed else None,
                    source=observed.source if observed else None,
                )
            )
            if observation_state != "fresh":
                reasons.append(
                    Reason(
                        code="observation-" + observation_state,
                        outcome="unknown",
                        path=path + ("observations", requirement.observation),
                        message=f"Required observation is {observation_state}. {requirement.remedy}",
                    )
                )
            elif not any(same(observed.value, candidate) for candidate in requirement.allowed):
                reasons.append(
                    Reason(
                        code="observation-condition",
                        outcome=requirement.on_mismatch,
                        path=path + ("observations", requirement.observation),
                        message=requirement.remedy,
                    )
                )
        if not reasons:
            reasons.append(
                Reason(
                    code="model-supports-request",
                    outcome="supported",
                    path=path,
                    message="Inputs meet the modeled constraints and evidence scope.",
                )
            )
        return Decision(
            outcome=outcome(reasons),
            reasons=tuple(reasons),
            assumptions=tuple(assumptions),
            evidence=tuple(uses),
            observations=tuple(observation_uses),
        )

    step_results: list[StepResult] = []
    extra_reasons: list[Reason] = []
    if request.target_kind == "capability":
        if request.target not in capabilities:
            raise KeyError("unknown capability")
        capability = capabilities[request.target]
        decision = decide(capability, initial_state)
        modeled_facts = {r.fact for r in capability.requirements}
    else:
        workflows = {w.id: w for w in artifact.shop.workflows}
        if request.target not in workflows:
            raise KeyError("unknown workflow")
        workflow = workflows[request.target]
        modeled_facts = {r.fact for r in workflow.requirements}
        extra_reasons.extend(
            reason
            for rule in workflow.requirements
            if (reason := check_rule(rule, facts, ("workflows", workflow.id, "requirements")))
        )
        steps = {step.id: step for step in workflow.steps}
        states: dict[str, dict[str, Value]] = {}
        for step_id in ordered_nodes({step.id: step.after for step in workflow.steps}):
            step = steps[step_id]
            capability = capabilities[step.capability]
            modeled_facts.update(r.fact for r in capability.requirements)
            state = initial_state.copy()
            if step.after:
                # State is propagated only from actual predecessors. Never
                # gather outputs of unrelated steps into a convenient union.
                state = {}
                conflicting = set()
                for parent in step.after:
                    for name, value in states[parent].items():
                        if name in state and not same(state[name], value):
                            conflicting.add(name)
                        else:
                            state[name] = value
                for name in sorted(conflicting):
                    state.pop(name, None)
                    extra_reasons.append(
                        Reason(
                            code="conflicting-input-state",
                            outcome="unsupported",
                            path=("workflows", workflow.id, "steps", step.id, name),
                            message="Predecessor outputs disagree about the same subject property.",
                        )
                    )
            step_decision = decide(capability, state)
            step_results.append(
                StepResult(step=step.id, capability=capability.id, decision=step_decision)
            )
            states[step.id] = state.copy()
            if step_decision.outcome in ("supported", "conditional"):
                states[step.id].update({f.name: f.value for f in capability.output_state})
            else:
                # An unproven transformation cannot establish an output.
                for fact in capability.output_state:
                    states[step.id].pop(fact.name, None)
        reasons = tuple(r for step in step_results for r in step.decision.reasons)
        decision = Decision(
            outcome=outcome(reasons),
            reasons=reasons,
            assumptions=tuple(f for s in step_results for f in s.decision.assumptions),
            evidence=tuple(e for s in step_results for e in s.decision.evidence),
            observations=tuple(o for s in step_results for o in s.decision.observations),
        )
    for name in sorted(facts.keys() - modeled_facts - declared_facts):
        extra_reasons.append(
            Reason(
                code="unmodeled-requirement",
                outcome="unknown",
                path=("facts", name),
                message=f"The model has no constraint for requested requirement {name}.",
            )
        )
    all_reasons = (*decision.reasons, *extra_reasons)
    return Evaluation(
        **decision.model_dump(exclude={"outcome", "reasons"}),
        outcome=outcome(all_reasons),
        reasons=all_reasons,
        target_kind=request.target_kind,
        target=request.target,
        model_sha256=artifact.model_sha256,
        evaluated_at=at,
        simulated=any(e.source == "simulated" for e in decision.evidence)
        or any(o.source == "simulated" for o in decision.observations),
        steps=tuple(step_results),
    )
