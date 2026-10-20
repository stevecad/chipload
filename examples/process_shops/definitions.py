"""Shared authoring components for the example projects, not engine extensions."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from chipload import Choice, Fact, Observation, Range, quantity
from chipload.model import Rule, Value
from chipload.project import AcceptanceCase

AT = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


def dimension(name: str, maximum: str, unit: str = "mm", minimum: str = "0.001") -> Range:
    return Range(fact=name, minimum=quantity(minimum, unit), maximum=quantity(maximum, unit))


def selection(name: str, *values: str) -> Choice:
    return Choice(fact=name, allowed=values)


def review(name: str) -> Choice:
    return Choice(fact=name, allowed=(True,), on_mismatch="unknown")


def facts(**values: Value) -> tuple[Fact, ...]:
    return tuple(Fact(name=name.replace("_", "-"), value=value) for name, value in values.items())


@dataclass(frozen=True)
class ProcessExample:
    id: str
    name: str
    family: str
    machine_kind: str
    tooling: str
    requirements: tuple[Rule, ...]
    inputs: tuple[Fact, ...]
    rejected: Fact
    review_fact: str
    policy: str
    limitation: str

    @property
    def shop_id(self) -> str:
        return f"demo-{self.id}"

    def observations(self, *, stale: bool = False, available: bool = True):
        time = AT - timedelta(hours=2) if stale else AT
        return (
            Observation(
                id=f"{self.id}-verification",
                resource=f"{self.id}-station",
                property="process-verification",
                value="current",
                time=time,
                max_age_seconds=3600,
                source="simulated",
            ),
            Observation(
                id=f"{self.id}-tooling-ready",
                resource=f"{self.id}-tooling",
                property="available",
                value=available,
                time=AT,
                max_age_seconds=3600,
                source="simulated",
            ),
        )


def changed(values: tuple[Fact, ...], **changes: Value | None) -> tuple[Fact, ...]:
    result = {fact.name: fact.value for fact in values}
    for name, value in changes.items():
        if value is None:
            result.pop(name, None)
        else:
            result[name] = value
    return tuple(Fact(name=name, value=value) for name, value in result.items())


def case(id, request, expected, reason, observations=()) -> AcceptanceCase:
    return AcceptanceCase(
        id=id,
        request=request,
        expected=expected,
        reason_codes=(reason,),
        at=AT,
        observations=observations,
    )
