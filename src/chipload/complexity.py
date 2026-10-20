"""Conservative data-work budgets, independent of request values or shop type.

These scores bound repeated comparisons and result construction. They are not
milliseconds or a replacement for process resource limits and deployment quotas.
"""

from .limits import MAX_EVALUATION_WORK, MAX_RESULT_WEIGHT, MAX_WORKFLOW_STATE
from .model import Choice, Range, Rule, Shop


def choice_work(values: tuple) -> int:
    return sum(1 + (len(value) // 64 if isinstance(value, str) else 0) for value in values)


def rule_work(rule: Rule) -> int:
    # Exact rational operations on up to 100 decimal digits cost more than a
    # scalar equality. Choices include worst-case unsuccessful linear searches.
    return 256 if isinstance(rule, Range) else 1 + choice_work(rule.allowed)


def rule_result(rule: Rule) -> int:
    return 256 + len(rule.remedy)


def capability_budgets(shop: Shop) -> dict[str, tuple[int, int]]:
    proofs = {proof.id: proof for proof in shop.evidence}
    result = {}
    for cap in shop.capabilities:
        work = 100 + sum(rule_work(r) for r in cap.requirements)
        work += len(cap.resources) + 64 * len(cap.input_state) + len(cap.output_state)
        weight = 256 + sum(rule_result(r) for r in cap.requirements)
        weight += 256 * (len(cap.resources) + len(cap.input_state))
        for rule in cap.requirements:
            if isinstance(rule, Choice) and len(rule.allowed) == 1:
                weight += 256 + len(str(rule.allowed[0]))
        for proof_id in cap.evidence:
            proof = proofs[proof_id]
            work += (
                len(cap.requirements) * 2
                + len(cap.resources)
                + len(proof.resources)
                + len(proof.requirements) * 2
                + sum(rule_work(r) for r in proof.requirements)
            )
            weight += 512
        for observation in cap.observations:
            work += 10 + choice_work(observation.allowed)
            weight += 768 + len(observation.remedy)
        result[cap.id] = work, weight
    return result


def target_budgets(shop: Shop) -> dict[tuple[str, str], tuple[int, int]]:
    capabilities = capability_budgets(shop)
    by_id = {cap.id: cap for cap in shop.capabilities}
    budgets = {("capability", key): value for key, value in capabilities.items()}
    for workflow in shop.workflows:
        work = sum(rule_work(r) for r in workflow.requirements)
        weight = sum(rule_result(r) for r in workflow.requirements)
        # Include every invocation, even when many steps reference one capability.
        fields = {f.name for s in workflow.steps for f in by_id[s.capability].output_state}
        state_size = 100 + len(fields)
        for step in workflow.steps:
            cap_work, cap_weight = capabilities[step.capability]
            # One-parent propagation copies data; joins also compare exact values.
            work += cap_work + state_size * (2 + len(step.after))
            work += max(0, len(step.after) - 1) * state_size * 64
            weight += cap_weight
            if len(step.after) > 1:
                weight += state_size * 256  # possible conflict reasons
        # Workflow results include both aggregate and per-step decisions.
        budgets["workflow", workflow.id] = work, weight * 2
    return budgets


def complexity_errors(shop: Shop):
    """Call only after identities, references, and graph validity are established."""
    by_id = {cap.id: cap for cap in shop.capabilities}
    for workflow in shop.workflows:
        fields = {f.name for s in workflow.steps for f in by_id[s.capability].output_state}
        if 100 + len(fields) > MAX_WORKFLOW_STATE:
            yield (
                ("workflows", workflow.id),
                (
                    f"Workflow state exceeds {MAX_WORKFLOW_STATE} fields including 100 request fields."
                ),
            )
    budgets = target_budgets(shop)
    for operation in shop.operations:
        work = sum(budgets[check.target_kind, check.target][0] for check in operation.checks)
        weight = sum(budgets[check.target_kind, check.target][1] for check in operation.checks)
        if work > MAX_EVALUATION_WORK or weight > MAX_RESULT_WEIGHT:
            yield (
                ("operations", operation.id),
                "Combined operation checks exceed the evaluation budget.",
            )
    for (kind, target), (work, weight) in budgets.items():
        path = ("capabilities" if kind == "capability" else "workflows", target)
        if work > MAX_EVALUATION_WORK:
            yield path, f"Evaluation work score {work} exceeds {MAX_EVALUATION_WORK}."
        if weight > MAX_RESULT_WEIGHT:
            yield path, f"Evaluation result weight {weight} exceeds {MAX_RESULT_WEIGHT}."
    variable_work = sum(rule_work(r) for v in shop.variables for r in v.applies_when)
    variable_weight = sum(rule_result(r) for v in shop.variables for r in v.applies_when)
    if variable_work > MAX_EVALUATION_WORK or variable_weight > MAX_RESULT_WEIGHT:
        yield ("variables",), "Combined variable applicability exceeds the evaluation budget."
