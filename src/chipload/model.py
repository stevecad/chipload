"""Versioned authoring objects and JSON schemas shared by future transports."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from typing import Annotated, Literal, NamedTuple, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_serializer,
    model_validator,
)

Id = Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$", max_length=64)]
Name = Annotated[StrictStr, Field(min_length=1, max_length=200)]
Text = Annotated[StrictStr, Field(max_length=4000)]
Visibility = Literal["private", "public"]
Assurance = Literal["declared", "demonstrated", "qualified"]
Source = Literal["controller", "sensor", "person", "calculated", "simulated"]
Entrypoint = Annotated[
    StrictStr, Field(pattern=r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$", max_length=200)
]


class Model(BaseModel):
    # Tuples and frozen nested models prevent mutation after validation.
    # Scalar types are strict; JSON arrays may become tuples.
    model_config = ConfigDict(
        extra="forbid", frozen=True, validate_default=True, revalidate_instances="always"
    )

    @model_validator(mode="before")
    @classmethod
    def collection_limit(cls, value: object) -> object:
        # Reject oversized arrays before validating each element or collecting
        # thousands of nested errors. Field metadata also exposes JSON Schema limits.
        if isinstance(value, dict):
            for name, field in cls.model_fields.items():
                items = value.get(name)
                if isinstance(items, (list, tuple)):
                    for constraint in field.metadata:
                        maximum = getattr(constraint, "max_length", None)
                        if maximum is not None and len(items) > maximum:
                            raise ValueError(f"{name} exceeds the limit of {maximum} items")
        return value


# A deliberately explicit initial UCUM subset. No guessing unknown unit strings.
# Rational conversion keeps boundary comparisons exact, independent of Decimal context.
class Unit(NamedTuple):
    dimension: str
    scale: Fraction
    offset: Fraction = Fraction(0)


UNITS: dict[str, Unit] = {
    "mm": Unit("length", Fraction(1)),
    "cm": Unit("length", Fraction(10)),
    "m": Unit("length", Fraction(1000)),
    "um": Unit("length", Fraction(1, 1000)),
    "[in_i]": Unit("length", Fraction(254, 10)),
    "g": Unit("mass", Fraction(1)),
    "kg": Unit("mass", Fraction(1000)),
    "s": Unit("time", Fraction(1)),
    "min": Unit("time", Fraction(60)),
    "h": Unit("time", Fraction(3600)),
    "Pa": Unit("pressure", Fraction(1)),
    "kPa": Unit("pressure", Fraction(1000)),
    "W": Unit("power", Fraction(1)),
    "kW": Unit("power", Fraction(1000)),
    "1": Unit("ratio", Fraction(1)),
    "K": Unit("temperature", Fraction(1)),
    "Cel": Unit("temperature", Fraction(1), Fraction(27315, 100)),
    "[degF]": Unit("temperature", Fraction(5, 9), Fraction(45967, 180)),
}


class Quantity(Model):
    value: Annotated[StrictStr, Field(max_length=100)]
    unit: Annotated[StrictStr, Field(max_length=16)]

    @field_validator("value")
    @classmethod
    def decimal_string(cls, value: str) -> str:
        if not re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value):
            raise ValueError("use a finite decimal string without an exponent")
        # Canonicalize without a rounding Decimal context.
        value = value.rstrip("0").rstrip(".") if "." in value else value
        return "0" if Decimal(value) == 0 else value

    @field_validator("unit")
    @classmethod
    def supported_unit(cls, unit: str) -> str:
        if unit not in UNITS:
            raise ValueError("unit is outside the supported UCUM subset")
        return unit

    @property
    def dimension(self) -> str:
        return UNITS[self.unit][0]

    def base_value(self) -> Fraction:
        definition = UNITS[self.unit]
        return Fraction(self.value) * definition.scale + definition.offset


def quantity(value: str, unit: str) -> Quantity:
    """Construct a measurement using an exact decimal string and a UCUM unit."""
    return Quantity(value=value, unit=unit)


Value = Quantity | Text | StrictBool


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a time zone")
    return value.astimezone(timezone.utc)


def timestamp_input(value: object) -> object:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise ValueError("use an aware datetime or an RFC 3339 timestamp with explicit zone")
    return value


Timestamp = Annotated[datetime, BeforeValidator(timestamp_input), AfterValidator(aware)]


class Fact(Model):
    name: Id
    value: Value


class Entity(Model):
    id: Id
    name: Name
    description: Text = ""
    visibility: Visibility = "private"


class Site(Entity):
    time_zone: Annotated[StrictStr, Field(max_length=100)] | None = None

    @field_validator("time_zone")
    @classmethod
    def known_zone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("unknown IANA time zone") from error
        return value


class Resource(Entity):
    # Classification belongs to the shop definition, not a platform shop taxonomy.
    kind: Id
    site: Id | None = None
    parent: Id | None = None
    status: Literal["active", "inactive", "retired"] = "active"
    # This field is never part of public discovery, even for a public resource.
    private_details: tuple[Fact, ...] = Field(default=(), max_length=100)


class CatalogItem(Entity):
    properties: tuple[Fact, ...] = Field(default=(), max_length=100)


class Catalog(Entity):
    """Shop-defined vocabulary: materials, document formats, services, or other inputs."""

    items: tuple[CatalogItem, ...] = Field(max_length=1000)


class Process(Entity):
    delivery: Id | None = None


class Evidence(Entity):
    level: Assurance
    source: Source
    claim: Text
    # Evidence is bound to the exact capability IDs it supports. Each capability
    # author must ensure its declared envelope is within this evidence's scope.
    capabilities: tuple[Id, ...] = Field(max_length=256)
    resources: tuple[Id, ...] = Field(default=(), max_length=256)
    requirements: tuple[Rule, ...] = Field(default=(), max_length=100)
    recorded_at: Timestamp
    valid_until: Timestamp | None = None
    reference: Text = ""

    @model_validator(mode="after")
    def dates(self) -> Self:
        if not self.capabilities:
            raise ValueError("evidence must name at least one capability")
        if self.valid_until is not None and self.valid_until <= self.recorded_at:
            raise ValueError("valid_until must be later than recorded_at")
        return self


class Constraint(Model):
    fact: Id
    on_mismatch: Literal["unsupported", "conditional", "unknown"] = "unsupported"
    on_missing: Literal["unknown", "conditional"] = "unknown"
    remedy: Text = ""

    @model_validator(mode="after")
    def conditional_has_remedy(self) -> Self:
        if "conditional" in (self.on_missing, self.on_mismatch) and not self.remedy.strip():
            raise ValueError("a conditional requirement needs an explicit remedy")
        return self


class Range(Constraint):
    kind: Literal["range"] = "range"
    minimum: Quantity | None = None
    maximum: Quantity | None = None
    # Omit the new optional field when absent so existing draft artifact digests
    # retain their meaning. An increment is measured from zero, not the minimum.
    multiple_of: Quantity | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if self.minimum is None and self.maximum is None:
            raise ValueError("a range needs at least one bound")
        if self.minimum is not None and self.maximum is not None:
            if self.minimum.dimension != self.maximum.dimension:
                raise ValueError("range bounds must have compatible units")
            if self.minimum.base_value() > self.maximum.base_value():
                raise ValueError("minimum must not exceed maximum")
        if self.multiple_of is not None:
            bound = self.minimum if self.minimum is not None else self.maximum
            if self.multiple_of.dimension != bound.dimension:
                raise ValueError("multiple_of must have the same dimension as the bounds")
            if self.multiple_of.dimension == "temperature":
                raise ValueError("absolute temperatures do not define an increment")
            if self.multiple_of.base_value() <= 0:
                raise ValueError("multiple_of must be positive")
        return self


class Choice(Constraint):
    kind: Literal["choice"] = "choice"
    allowed: tuple[Text | StrictBool, ...] = Field(max_length=256)
    catalog: Id | None = None

    @model_validator(mode="after")
    def choices(self) -> Self:
        if not self.allowed or len(set(self.allowed)) != len(self.allowed):
            raise ValueError("choices must be nonempty and unique")
        if self.catalog is not None and not all(isinstance(value, str) for value in self.allowed):
            raise ValueError("catalog choices must use item identifiers")
        return self


Rule = Annotated[Range | Choice, Field(discriminator="kind")]


class ObservationRequirement(Model):
    observation: Id
    resource: Id
    property: Id
    allowed: tuple[Text | StrictBool, ...] = Field(max_length=256)
    purpose: Literal["capability", "readiness"] = "capability"
    on_mismatch: Literal["unsupported", "conditional", "unknown"] = "conditional"
    remedy: Text

    @model_validator(mode="after")
    def choices(self) -> Self:
        if not self.allowed or len(set(self.allowed)) != len(self.allowed):
            raise ValueError("observation choices must be nonempty and unique")
        if not self.remedy.strip():
            raise ValueError("an observation requirement needs a remedy")
        return self


class PythonCheck(Entity):
    """A source function constraining named request facts; executed only in a worker."""

    entrypoint: Entrypoint
    facts: tuple[Id, ...] = Field(min_length=1, max_length=100)
    remedy: Text = ""

    @model_validator(mode="after")
    def unique_inputs(self) -> Self:
        if len(set(self.facts)) != len(self.facts):
            raise ValueError("Python check inputs must be unique")
        return self


class Capability(Entity):
    process: Id
    resources: tuple[Id, ...] = Field(default=(), max_length=256)
    requirements: tuple[Rule, ...] = Field(default=(), max_length=100)
    evidence: tuple[Id, ...] = Field(default=(), max_length=32)
    observations: tuple[ObservationRequirement, ...] = Field(default=(), max_length=64)
    input_state: tuple[Fact, ...] = Field(default=(), max_length=100)
    output_state: tuple[Fact, ...] = Field(default=(), max_length=100)
    python_checks: tuple[PythonCheck, ...] = Field(
        default=(), max_length=8, exclude_if=lambda value: not value
    )


class Step(Model):
    id: Id
    capability: Id
    after: tuple[Id, ...] = Field(default=(), max_length=64)


class Workflow(Entity):
    steps: tuple[Step, ...] = Field(max_length=256)
    # Restrict the requests a workflow promises to handle, in addition to the
    # requirements of each constituent capability.
    requirements: tuple[Rule, ...] = Field(default=(), max_length=100)


class Route(Model):
    """One concrete capability or workflow considered by an offer."""

    target_kind: Literal["capability", "workflow"]
    target: Id


class Offer(Entity):
    """Alternative routes to a shop-defined outcome, with shared conditions."""

    alternatives: tuple[Route, ...] = Field(min_length=1, max_length=32)
    requirements: tuple[Rule, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def unique_routes(self) -> Self:
        if len({(r.target_kind, r.target) for r in self.alternatives}) != len(self.alternatives):
            raise ValueError("offer alternatives must be unique")
        return self


class Interval(Model):
    """A reported range, not automatically an enforceable manufacturing limit."""

    kind: Literal["interval"] = "interval"
    minimum: Quantity
    maximum: Quantity

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if self.minimum.dimension != self.maximum.dimension:
            raise ValueError("interval bounds must have compatible units")
        if self.minimum.base_value() > self.maximum.base_value():
            raise ValueError("minimum must not exceed maximum")
        return self


class ValueSet(Model):
    kind: Literal["set"] = "set"
    values: tuple[Text, ...] = Field(max_length=256)

    @model_validator(mode="after")
    def unique(self) -> Self:
        if not self.values or len(set(self.values)) != len(self.values):
            raise ValueError("a value set must be nonempty and unique")
        return self


VariableValue = Value | Interval | ValueSet | None


class ShopVariable(Entity):
    """A shop fact or policy with its original certainty, scope, and provenance.

    Agents may compose variables with Python. Publishing a descriptive variable
    does not turn it into an equipment action or a guaranteed service promise.
    """

    category: Id
    value: VariableValue
    certainty: Literal["declared", "approximate", "typical", "unknown"]
    source: Source
    recorded_at: Timestamp
    valid_until: Timestamp | None = None
    applies_when: tuple[Rule, ...] = Field(default=(), max_length=100)
    notes: Text = ""

    @model_validator(mode="after")
    def meaning(self) -> Self:
        if (self.value is None) != (self.certainty == "unknown"):
            raise ValueError("unknown variables use null; known values need a stated certainty")
        if self.valid_until is not None and self.valid_until <= self.recorded_at:
            raise ValueError("valid_until must be later than recorded_at")
        for rule in self.applies_when:
            if rule.on_mismatch != "unsupported" or rule.on_missing != "unknown":
                raise ValueError("applicability predicates cannot assume conditional remedies")
        return self


class OperationField(Model):
    """A named operation argument or result; quantities retain explicit units."""

    name: Id
    kind: Literal["text", "boolean", "quantity"]
    description: Text = ""
    required: StrictBool = True
    unit: Annotated[StrictStr, Field(max_length=100)] | None = None

    @model_validator(mode="after")
    def measurement(self) -> Self:
        if self.kind == "quantity":
            if self.unit not in UNITS:
                raise ValueError("quantity fields need a supported unit")
        elif self.unit is not None:
            raise ValueError("only quantity fields have a unit")
        return self


class OperationCheck(Model):
    """Evaluate a published capability or workflow before dispatching an operation."""

    target_kind: Literal["capability", "workflow"]
    target: Id
    inputs: tuple[Id, ...] = Field(default=(), max_length=100)
    initial_state: tuple[Fact, ...] = Field(default=(), max_length=100)
    minimum_evidence: Assurance = "qualified"


class ShopOperation(Entity):
    """A shop-authored call contract, independent of privileged adapter provisioning."""

    adapter: Id
    command: Id
    effect: Literal["read", "write"]
    effect_description: Annotated[StrictStr, Field(min_length=1, max_length=4000)]
    resources: tuple[Id, ...] = Field(default=(), max_length=64)
    inputs: tuple[OperationField, ...] = Field(default=(), max_length=100)
    outputs: tuple[OperationField, ...] = Field(default=(), max_length=100)
    checks: tuple[OperationCheck, ...] = Field(default=(), max_length=16)
    timeout_seconds: Annotated[StrictInt, Field(ge=1, le=300)] = 30


class Shop(Entity):
    schema_version: Literal["0.2-draft"] = "0.2-draft"
    sites: tuple[Site, ...] = Field(default=(), max_length=1000)
    resources: tuple[Resource, ...] = Field(default=(), max_length=1000)
    catalogs: tuple[Catalog, ...] = Field(default=(), max_length=1000)
    processes: tuple[Process, ...] = Field(default=(), max_length=1000)
    capabilities: tuple[Capability, ...] = Field(default=(), max_length=1000)
    workflows: tuple[Workflow, ...] = Field(default=(), max_length=1000)
    offers: tuple[Offer, ...] = Field(default=(), max_length=1000, exclude_if=lambda value: not value)
    evidence: tuple[Evidence, ...] = Field(default=(), max_length=1000)
    variables: tuple[ShopVariable, ...] = Field(default=(), max_length=1000)
    operations: tuple[ShopOperation, ...] = Field(default=(), max_length=1000)

    @model_serializer(mode="wrap")
    def preserve_existing_artifacts(self, handler):
        result = handler(self)
        if not self.operations:
            result.pop("operations", None)
        return result


class ExternalReference(Model):
    """Opaque source identity; never a URL to fetch or an ERP record owned here."""

    system: Id
    record: Annotated[StrictStr, Field(min_length=1, max_length=512)]
    revision: Annotated[StrictStr, Field(min_length=1, max_length=200)] | None = None

    @model_validator(mode="after")
    def nonempty_reference(self) -> Self:
        if not self.record.strip() or (self.revision is not None and not self.revision.strip()):
            raise ValueError("External record references must not be blank")
        return self


class Observation(Model):
    id: Id
    resource: Id
    property: Id
    value: Value | None
    time: Timestamp
    max_age_seconds: Annotated[StrictInt, Field(gt=0, le=31536000)]
    source: Source
    external: ExternalReference | None = Field(default=None, exclude_if=lambda value: value is None)

    @property
    def expires_at(self) -> datetime:
        try:
            return self.time + timedelta(seconds=self.max_age_seconds)
        except OverflowError:
            return datetime.max.replace(tzinfo=timezone.utc)
