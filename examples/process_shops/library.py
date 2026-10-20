"""Example-project index. The Chipload engine never imports this registry."""

from . import (
    abrasive_and_cutting,
    additive_and_molding,
    forming_and_joining,
    machining,
    treatment_and_inspection,
)

EXAMPLES = (
    *machining.EXAMPLES,
    *abrasive_and_cutting.EXAMPLES,
    *forming_and_joining.EXAMPLES,
    *additive_and_molding.EXAMPLES,
    *treatment_and_inspection.EXAMPLES,
)
BY_ID = {example.id: example for example in EXAMPLES}
if len(BY_ID) != len(EXAMPLES):
    raise ValueError("example identifiers must be unique")
