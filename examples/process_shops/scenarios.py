"""Acceptance examples that distinguish technical capability from business acceptance."""

from chipload import EvaluationRequest, Fact, quantity

from .business import TERMS
from .business import inputs as business_inputs
from .definitions import ProcessExample, case, changed


def technical_inputs(example: ProcessExample):
    return (
        Fact(name="input-grade", value="demo-approved-input"),
        *example.inputs,
        Fact(name=f"{example.id}-setup", value="verified"),
    )


def request(example: ProcessExample, *, engagement=None, changes=None, **options):
    values = technical_inputs(example)
    if engagement:
        values += business_inputs(TERMS[example.policy], engagement)
    return EvaluationRequest(
        target_kind="workflow" if engagement else "capability",
        target=f"offer-{engagement}" if engagement else example.id,
        facts=changed(values, **(changes or {})),
        initial_state=(Fact(name="traceability", value="identified"),),
        allow_simulated=True,
        **options,
    )


def scenarios(example: ProcessExample):
    observations = example.observations()
    terms = TERMS[example.policy]

    def technical(id, expected, reason, changes=None, *, snapshot=observations, **options):
        return case(id, request(example, changes=changes, **options), expected, reason, snapshot)

    def commercial(id, expected, reason, changes=None, engagement="production"):
        return case(
            id,
            request(example, engagement=engagement, changes=changes),
            expected,
            reason,
            observations,
        )

    result = [
        technical("technical-supported", "supported", "model-supports-request"),
        technical(
            "process-specific-rejection",
            "unsupported",
            "outside-range" if hasattr(example.rejected.value, "unit") else "value-not-supported",
            {example.rejected.name: example.rejected.value},
        ),
        technical("missing-process-review", "unknown", "missing-fact", {example.review_fact: None}),
        technical(
            "unreviewed-process", "unknown", "value-not-supported", {example.review_fact: False}
        ),
        technical(
            "unlisted-input",
            "unsupported",
            "value-not-supported",
            {"input-grade": "unqualified-input"},
        ),
        technical(
            "setup-needed",
            "conditional",
            "value-not-supported",
            {f"{example.id}-setup": "not-prepared"},
        ),
        technical(
            "stale-verification",
            "unknown",
            "observation-stale",
            snapshot=example.observations(stale=True),
        ),
        technical(
            "tooling-busy-for-readiness",
            "conditional",
            "observation-condition",
            snapshot=example.observations(available=False),
            purpose="readiness",
        ),
        technical(
            "capability-is-not-availability",
            "supported",
            "model-supports-request",
            snapshot=example.observations(available=False),
        ),
        technical(
            "customer-qualification-unproven",
            "unknown",
            "evidence-unavailable",
            minimum_evidence="qualified",
        ),
        technical(
            "unmodeled-performance",
            "unknown",
            "unmodeled-requirement",
            {"unmodeled-performance": "customer-required-outcome"},
        ),
        commercial("production-accepted", "supported", "model-supports-request"),
        commercial(
            "prototype-exception", "supported", "model-supports-request", engagement="prototype"
        ),
        commercial(
            "production-below-minimum",
            "unsupported",
            "outside-range",
            {"lot-quantity": quantity("1", "1")},
        ),
        commercial(
            "prototype-above-limit",
            "unsupported",
            "outside-range",
            {"lot-quantity": quantity("1000", "1")},
            engagement="prototype",
        ),
        commercial(
            "fractional-part-count",
            "unsupported",
            "invalid-increment",
            {"lot-quantity": quantity("250.5", "1")},
        ),
        commercial(
            "incoming-paperwork-needed",
            "conditional",
            "value-not-supported",
            {"input-records-provided": False},
        ),
        commercial(
            "missing-business-quantity",
            "unknown",
            "missing-fact",
            {"lot-quantity": None},
        ),
        commercial(
            "schedule-not-promised",
            "conditional",
            "value-not-supported",
            {"schedule-confirmed": False},
        ),
        commercial(
            "customer-requirements-unreviewed",
            "unknown",
            "value-not-supported",
            {"customer-requirements-reviewed": False},
        ),
        commercial(
            "unsupported-documentation",
            "unsupported",
            "value-not-supported",
            {"documentation-package": "unqualified-customer-certificate"},
        ),
        commercial(
            "customer-supplied-inputs",
            "supported" if terms.customer_stock else "unsupported",
            "model-supports-request" if terms.customer_stock else "value-not-supported",
            {"input-ownership": "customer-supplied"},
        ),
        commercial(
            "monthly-production-releases",
            "supported" if terms.releases else "unsupported",
            "model-supports-request" if terms.releases else "value-not-supported",
            {"delivery-arrangement": "monthly-releases"},
        ),
        commercial(
            "prototype-no-recurring-release",
            "unsupported",
            "value-not-supported",
            {"delivery-arrangement": "monthly-releases"},
            engagement="prototype",
        ),
    ]
    if terms.outside_processing:
        result.append(
            commercial(
                "outside-processing-needs-authorization",
                "conditional",
                "value-not-supported",
                {"outside-processing-authorized": False},
            )
        )
    return tuple(result)
