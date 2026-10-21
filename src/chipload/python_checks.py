"""Data contracts for source-defined checks; this module never imports shop code."""

from typing import Literal

from pydantic import Field

from .artifact import Digest, canonical_json, digest
from .model import Id, Model, Timestamp

MAX_TARGET_CHECKS = 128
MAX_CASE_CHECKS = 512


class PythonCheckResult(Model):
    capability: Id
    check: Id
    outcome: Literal["supported", "unsupported", "conditional", "unknown"]


class PythonEvaluation(Model):
    build_sha256: Digest
    request_sha256: Digest
    evaluated_at: Timestamp
    results: tuple[PythonCheckResult, ...] = Field(max_length=MAX_TARGET_CHECKS)


class CaseChecks(Model):
    id: Id
    results: tuple[PythonCheckResult, ...] = Field(max_length=MAX_TARGET_CHECKS)


class CheckLimit(ValueError):
    def __init__(self, kind, target):
        self.kind, self.target = kind, target
        super().__init__(f"Evaluation requires more than {MAX_TARGET_CHECKS} Python checks")


def check_plans(shop):
    """Unique checks per target. Checks see facts, never mutable workflow state."""
    if not any(cap.python_checks for cap in shop.capabilities):
        return {}
    plans = {
        ("capability", cap.id): {(cap.id, check.id): check for check in cap.python_checks}
        for cap in shop.capabilities
    }

    def merge(kind, identifier, routes):
        checks = {}
        for route in routes:
            checks.update(plans[route])
            if len(checks) > MAX_TARGET_CHECKS:
                raise CheckLimit(kind, identifier)
        plans[kind, identifier] = checks

    for workflow in shop.workflows:
        merge("workflow", workflow.id, (("capability", step.capability) for step in workflow.steps))
    for offer in shop.offers:
        merge(
            "offer", offer.id, ((route.target_kind, route.target) for route in offer.alternatives)
        )
    return plans


def suite_plans(shop, cases):
    plans = check_plans(shop)
    selected = [plans.get((case.request.target_kind, case.request.target), {}) for case in cases]
    if sum(map(len, selected)) > MAX_CASE_CHECKS:
        raise ValueError(f"Acceptance suite exceeds {MAX_CASE_CHECKS} Python check calls")
    return selected


def validate_check_sources(shop, project):
    paths = {file.path for file in project.files}
    for capability in shop.capabilities:
        for check in capability.python_checks:
            module = check.entrypoint.split(":")[0].replace(".", "/")
            if f"{module}.py" not in paths and f"{module}/__init__.py" not in paths:
                raise ValueError(f"Python check {capability.id}/{check.id} needs a supplied module")


def require_check_coverage(shop, cases):
    plans = suite_plans(shop, cases)
    covered = {key for plan in plans for key in plan}
    needed = {(cap.id, check.id) for cap in shop.capabilities for check in cap.python_checks}
    if missing := needed - covered:
        capability, check = min(missing)
        raise ValueError(f"Python check {capability}/{check} needs an acceptance case")
    return plans


def validated_outcomes(artifact, request, at, evaluation):
    if evaluation is None:
        return {}
    evaluation = PythonEvaluation.model_validate(evaluation)
    if (evaluation.build_sha256, evaluation.request_sha256, evaluation.evaluated_at) != (
        artifact.build_sha256,
        digest(canonical_json(request)),
        at,
    ):
        raise ValueError("Python check results do not match this build, query and time")
    needed = check_plans(artifact.shop).get((request.target_kind, request.target), {})
    results = {(item.capability, item.check): item.outcome for item in evaluation.results}
    if len(results) != len(evaluation.results) or results.keys() != needed.keys():
        raise ValueError("Python check results must cover exactly the requested checks")
    if any(
        value == "conditional" and not needed[key].remedy.strip() for key, value in results.items()
    ):
        raise ValueError("A conditional Python check needs a declared remedy")
    return results


def bind_case_results(artifact, cases, values):
    if not isinstance(values, list) or len(values) != len(cases):
        raise ValueError("Python worker results must match the acceptance cases")
    results = []
    for case, value in zip(cases, values, strict=True):
        record = CaseChecks.model_validate(value)
        if record.id != case.id:
            raise ValueError("Python worker case identity mismatch")
        evaluation = PythonEvaluation(
            build_sha256=artifact.build_sha256,
            request_sha256=digest(canonical_json(case.request)),
            evaluated_at=case.at,
            results=record.results,
        )
        validated_outcomes(artifact, case.request, case.at, evaluation)
        results.append(evaluation)
    return tuple(results)
