"""Container-only function invocation. Never call this from an API or broker."""

import importlib

from .model import Fact
from .python_checks import (
    CaseChecks,
    PythonCheckResult,
    require_check_coverage,
    validate_check_sources,
)


class CheckExecutionError(ValueError):
    def __init__(self, case, capability, check, error):
        self.path = ("tests", case, "python_checks", capability, check)
        super().__init__(f"{type(error).__name__}: {error}"[:1000])


def run_checks(shop, project):
    validate_check_sources(shop, project)
    plans = require_check_coverage(shop, project.tests)
    answers = []
    for case, plan in zip(project.tests, plans, strict=True):
        values = {fact.name: fact for fact in case.request.facts}
        results = []
        for (capability, identifier), check in sorted(plan.items()):
            try:
                module, function = check.entrypoint.split(":")
                callback = getattr(importlib.import_module(module), function)
                # Each call gets fresh declared facts without service authority.
                inputs = {
                    name: Fact.model_validate(values[name].model_dump()).value
                    if name in values
                    else None
                    for name in check.facts
                }
                result = PythonCheckResult(
                    capability=capability, check=identifier, outcome=callback(inputs)
                )
                if result.outcome == "conditional" and not check.remedy.strip():
                    raise ValueError("A conditional Python check needs a declared remedy")
            except Exception as error:
                raise CheckExecutionError(case.id, capability, identifier, error) from error
            results.append(result)
        answers.append(CaseChecks(id=case.id, results=tuple(results)).model_dump(mode="json"))
    return answers
