"""Static machine-readable authoring reference; never imports shop projects."""

from .artifact import SDK_VERSION
from .evaluate import EvaluationRequest
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
        },
        "units": {
            name: {
                "dimension": unit.dimension,
                "scale": str(unit.scale),
                "offset": str(unit.offset),
            }
            for name, unit in UNITS.items()
        },
        "rules": [
            "Shop-specific types and vocabularies belong in the source project.",
            "Supply explicit units and timezone-aware timestamps.",
            "Objects are private by default; only public projections may be shared.",
            "Missing knowledge must remain unknown; simulation must remain explicit.",
            "Save, build, test, and publish are separate actions.",
            "Publication requires a successful build with at least one acceptance case.",
            "Source runs in an operator-provisioned environment with pinned dependencies.",
        ],
    }
