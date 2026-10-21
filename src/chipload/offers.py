"""Assess alternative concrete routes without combining their evidence or state."""

from .evaluate import (
    RANK,
    AlternativeResult,
    Decision,
    Evaluation,
    Reason,
    _evaluate,
    check_rule,
    outcome,
)


def evaluate_offer(artifact, request, *, at, observations, revoked_evidence, python_results):
    offer = next((item for item in artifact.shop.offers if item.id == request.target), None)
    if offer is None:
        raise KeyError("unknown offer")
    shared_facts = frozenset(rule.fact for rule in offer.requirements)
    alternatives = []
    for index, route in enumerate(offer.alternatives):
        # Shared conditions are checked once at offer level. Preserve their input
        # values for each route's evidence scope and any stricter requirements.
        query = request.model_copy(
            update={
                "target_kind": route.target_kind,
                "target": route.target,
            }
        )
        result = _evaluate(
            artifact, query, at=at, observations=observations, declared_facts=shared_facts,
            revoked_evidence=revoked_evidence,
            python_results=python_results,
        )
        reasons = tuple(r for r in result.reasons if r.code != "unmodeled-requirement")
        unmodeled = len(result.reasons) - len(reasons)
        if unmodeled:
            # Avoid repeating up to 100 unknown-field diagnostics in every route.
            reasons += (
                Reason(
                    code="unmodeled-requirement",
                    outcome="unknown",
                    path=("offers", offer.id, "alternatives", str(index), "facts"),
                    message=f"This route does not model {unmodeled} additional request fields.",
                ),
            )
        alternatives.append(
            AlternativeResult(
                **route.model_dump(),
                decision=Decision(
                    reasons=reasons,
                    **result.model_dump(
                        include={
                            "outcome",
                            "assumptions",
                            "evidence",
                            "observations",
                        }
                    ),
                ),
                steps=result.steps,
                simulated=result.simulated,
            )
        )
    # Declaration order breaks ties. This is an assessment, never dispatch,
    # scheduling, cost optimization, or a reservation of a resource.
    best_index = min(
        range(len(alternatives)), key=lambda index: RANK[alternatives[index].decision.outcome]
    )
    best = alternatives[best_index]
    facts = {fact.name: fact.value for fact in request.facts}
    shared_reasons = tuple(
        reason
        for rule in offer.requirements
        if (reason := check_rule(rule, facts, ("offers", offer.id, "requirements")))
    )
    route_reason = Reason(
        code="alternative-" + best.decision.outcome,
        outcome=best.decision.outcome,
        path=("offers", offer.id, "alternatives"),
        message={
            "supported": "At least one complete route supports these inputs.",
            "conditional": "At least one complete route has explicit conditions to resolve.",
            "unknown": "No route establishes support; at least one route lacks sufficient information.",
            "unsupported": "Every declared route rejects these inputs.",
        }[best.decision.outcome],
    )
    reasons = (*best.decision.reasons, *shared_reasons, route_reason)
    result_outcome = outcome(reasons)
    return Evaluation(
        target_kind="offer",
        target=offer.id,
        model_sha256=artifact.model_sha256,
        evaluated_at=at,
        outcome=result_outcome,
        reasons=reasons,
        **best.decision.model_dump(include={"assumptions", "evidence", "observations"}),
        simulated=best.simulated,
        alternatives=tuple(alternatives),
        matching_alternative=offer.alternatives[best_index]
        if result_outcome in {"supported", "conditional"}
        else None,
    )
