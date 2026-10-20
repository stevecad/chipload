"""Static machine-readable authoring reference; never imports shop projects."""

from .artifact import SDK_VERSION
from .evaluate import EvaluationRequest
from .execution_model import ExecutionRequest
from .limits import (
    MAX_CASE_WORK,
    MAX_DIAGNOSTICS,
    MAX_EVALUATION_WORK,
    MAX_JSON_DEPTH,
    MAX_JSON_TOKENS,
    MAX_MODEL_TOKENS,
    MAX_RESULT_WEIGHT,
    MAX_WORKFLOW_STATE,
)
from .model import UNITS, Shop
from .project import SourceProject
from .query import VariableQuery


def sdk_reference() -> dict:
    return {
        "sdk_version": SDK_VERSION,
        "entrypoint_contract": "A zero-argument function in a supplied module returns chipload.Shop.",
        "example": (
            "from chipload import Shop\n"
            "def define_shop():\n"
            "    return Shop(id='example', name='Example shop', visibility='public')\n"
        ),
        "schemas": {
            "shop": Shop.model_json_schema(),
            "source_project": SourceProject.model_json_schema(),
            "evaluation_request": EvaluationRequest.model_json_schema(),
            "variable_query": VariableQuery.model_json_schema(),
            "execution_request": ExecutionRequest.model_json_schema(),
        },
        "units": {
            name: {
                "dimension": unit.dimension,
                "scale": str(unit.scale),
                "offset": str(unit.offset),
            }
            for name, unit in UNITS.items()
        },
        "limits": {
            "json_depth": MAX_JSON_DEPTH,
            "json_structural_tokens": MAX_JSON_TOKENS,
            "model_structural_tokens": MAX_MODEL_TOKENS,
            "diagnostics": MAX_DIAGNOSTICS,
            "evaluation_work_score": MAX_EVALUATION_WORK,
            "evaluation_result_weight": MAX_RESULT_WEIGHT,
            "workflow_state_fields": MAX_WORKFLOW_STATE,
            "acceptance_suite_work_score": MAX_CASE_WORK,
            "collection_limits": "See maxItems and maxLength in the schemas.",
            "budget_meaning": (
                "Conservative deterministic work scores, not elapsed time. "
                "Repeated steps count repeatedly. Exceeding a budget rejects the build; "
                "it is not a manufacturing feasibility outcome."
            ),
        },
        "rules": [
            "Shop-specific types and vocabularies belong in the source project.",
            "Supply explicit units and timezone-aware timestamps.",
            "Objects are private by default; only public projections may be shared.",
            "Missing knowledge must remain unknown; simulation must remain explicit.",
            "Save, build, test, and publish are separate actions.",
            "Publication requires a successful build with at least one acceptance case.",
            "Source runs in an operator-provisioned environment with pinned dependencies.",
            "Operation contracts require a separate adapter binding to execute; publishing source does not grant equipment authority.",
            "Execution mode is explicit. Simulated results are not equipment qualification.",
            "Retry an exact execution request with its original request key. Reconcile indeterminate outcomes without resending effects.",
        ],
    }
