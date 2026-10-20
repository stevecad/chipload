"""Manufacturing questions with expectations independent of evaluator output."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from chipload import EvaluationRequest, Fact, Observation, quantity

AT = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


def request(
    target: str = "mill-small",
    *,
    target_kind: str = "capability",
    material: str = "al-6061-t6",
    purpose: str = "capability",
    changes: dict | None = None,
) -> EvaluationRequest:
    facts = {
        "length": quantity("100", "mm"),
        "width": quantity("60", "mm"),
        "height": quantity("20", "mm"),
        "tolerance": quantity("0.10", "mm"),
        "geometry-reviewed": True,
        "fixture": "flat-vise",
    }
    if target == "anodize":
        facts = {k: v for k, v in facts.items() if k in ("length", "width", "height")}
        facts["supplier-confirmed"] = True
    elif target == "inspect-bare":
        facts.pop("geometry-reviewed")
        facts.pop("fixture")
        facts["inspection-plan-reviewed"] = True
    elif target_kind == "workflow":
        facts.update(
            {
                "edge-plan-reviewed": True,
                "inspection-plan-reviewed": True,
                "supplier-confirmed": True,
            }
        )
        if target == "finished-bracket":
            facts["finish-allowance-reviewed"] = True
    for key, value in (changes or {}).items():
        if value is None:
            facts.pop(key, None)
        else:
            facts[key] = value
    facts["material"] = material
    return EvaluationRequest(
        target_kind=target_kind,
        target=target,
        purpose=purpose,
        facts=tuple(Fact(name=name, value=value) for name, value in facts.items()),
        initial_state=(
            Fact(name="surface", value="bare"),
            Fact(name="geometry", value="machined"),
            Fact(name="edges", value="deburred"),
        ),
        allow_simulated=True,
    )


def observations() -> tuple[Observation, ...]:
    return (
        Observation(
            id="mill-a-available",
            resource="mill-a",
            property="available",
            value=False,
            time=AT - timedelta(minutes=1),
            max_age_seconds=300,
            source="simulated",
        ),
        Observation(
            id="shared-endmill-available",
            resource="shared-endmill",
            property="available",
            value=True,
            time=AT - timedelta(minutes=1),
            max_age_seconds=300,
            source="simulated",
        ),
        Observation(
            id="aluminium-stock-available",
            resource="stock-al6061",
            property="stock-available",
            value=True,
            time=AT - timedelta(minutes=1),
            max_age_seconds=300,
            source="simulated",
        ),
        Observation(
            id="cmm-calibration",
            resource="cmm",
            property="calibration",
            value="valid",
            time=AT - timedelta(minutes=5),
            max_age_seconds=3600,
            source="simulated",
        ),
    )


@dataclass(frozen=True)
class Scenario:
    name: str
    question: EvaluationRequest
    expected: str
    reason: str
    explanation: str
    observation_snapshot: tuple[Observation, ...] = ()


SCENARIOS = (
    Scenario(
        "supported-bracket",
        request(),
        "supported",
        "model-supports-request",
        "The reviewed geometry and flat-vise setup fit the recorded aluminium envelope.",
    ),
    Scenario(
        "incompatible-material",
        request(material="ti-6al-4v"),
        "unsupported",
        "value-not-supported",
        "This process declaration covers aluminium, not titanium.",
    ),
    Scenario(
        "part-too-long",
        request(changes={"length": quantity("301", "mm")}),
        "unsupported",
        "outside-range",
        "The declared setup allows at most 300 mm length.",
    ),
    Scenario(
        "fixture-change",
        request("mill-wide", changes={"length": quantity("400", "mm")}),
        "conditional",
        "value-not-supported",
        "The wider machine can accommodate this part after setting up the wide fixture.",
    ),
    Scenario(
        "supplier-confirmation",
        request("anodize", changes={"supplier-confirmed": None}),
        "conditional",
        "missing-fact",
        "The outside supplier must accept the work.",
    ),
    Scenario(
        "complete-workflow",
        request("finished-bracket", target_kind="workflow"),
        "supported",
        "model-supports-request",
        "The modeled intermediate states are compatible.",
        observations(),
    ),
    Scenario(
        "incompatible-workflow",
        request("invalid-finish-before-inspection", target_kind="workflow"),
        "unsupported",
        "incompatible-input-state",
        "The bare-part inspection plan cannot accept an anodized part.",
        observations(),
    ),
    Scenario(
        "geometry-unknown",
        request(changes={"geometry-reviewed": None}),
        "unknown",
        "missing-fact",
        "A bounding box alone does not establish machinability.",
    ),
    Scenario(
        "tolerance-unqualified",
        request().model_copy(update={"minimum_evidence": "qualified"}),
        "unknown",
        "evidence-unavailable",
        "A declaration is not a qualification.",
    ),
    Scenario(
        "missing-calibration",
        request("inspect-bare"),
        "unknown",
        "observation-missing",
        "The required calibration observation is absent.",
    ),
    Scenario(
        "stale-calibration",
        request("inspect-bare"),
        "unknown",
        "observation-stale",
        "An old valid value cannot establish current calibration.",
        tuple(
            o.model_copy(update={"time": AT - timedelta(hours=2)})
            if o.id == "cmm-calibration"
            else o
            for o in observations()
        ),
    ),
    Scenario(
        "busy-but-capable",
        request(),
        "supported",
        "model-supports-request",
        "Availability is not part of the installed capability query.",
        observations(),
    ),
    Scenario(
        "busy-not-ready",
        request(purpose="readiness"),
        "conditional",
        "observation-condition",
        "The machine is busy, so immediate work needs scheduling.",
        observations(),
    ),
    Scenario(
        "stale-stock",
        request(purpose="readiness"),
        "unknown",
        "observation-stale",
        "A previous stock check cannot establish current material availability.",
        tuple(
            o.model_copy(update={"time": AT - timedelta(hours=2)})
            if o.id == "aluminium-stock-available"
            else o
            for o in observations()
        ),
    ),
    Scenario(
        "unmodeled-requirement",
        request(changes={"surface-roughness": quantity("0.8", "um")}),
        "unknown",
        "unmodeled-requirement",
        "The evaluator cannot silently ignore a requirement outside this model.",
    ),
    Scenario(
        "simulation-not-production",
        request().model_copy(update={"allow_simulated": False}),
        "unknown",
        "evidence-unavailable",
        "Simulated evidence cannot prove a real capability.",
    ),
)
