"""Chipload's Python authoring model.

Shop projects are ordinary Python programs. Execute them only in a trusted local
environment or a restricted build worker, never in a discovery request handler.
"""

from .artifact import Artifact, BuildIdentity, compile_shop, read_artifact
from .evaluate import Evaluation, EvaluationRequest, evaluate
from .model import (
    Capability,
    Catalog,
    CatalogItem,
    Choice,
    Evidence,
    Fact,
    Interval,
    Observation,
    ObservationRequirement,
    Offer,
    OperationCheck,
    OperationField,
    Process,
    Quantity,
    Range,
    Resource,
    Route,
    Shop,
    ShopOperation,
    ShopVariable,
    Site,
    Step,
    ValueSet,
    Workflow,
    quantity,
)
from .query import VariableQuery, VariableResult, query_variables
from .validation import Diagnostic, ShopValidationError, validate_shop

__all__ = [
    "Artifact",
    "BuildIdentity",
    "Capability",
    "Catalog",
    "CatalogItem",
    "Choice",
    "Diagnostic",
    "Evaluation",
    "EvaluationRequest",
    "Evidence",
    "Fact",
    "Interval",
    "Observation",
    "ObservationRequirement",
    "Offer",
    "OperationCheck",
    "OperationField",
    "Process",
    "Quantity",
    "Range",
    "Resource",
    "Route",
    "Shop",
    "ShopOperation",
    "ShopValidationError",
    "ShopVariable",
    "Site",
    "Step",
    "ValueSet",
    "VariableQuery",
    "VariableResult",
    "Workflow",
    "compile_shop",
    "evaluate",
    "quantity",
    "query_variables",
    "read_artifact",
    "validate_shop",
]
