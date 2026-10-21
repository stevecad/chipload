"""Transport-independent operation contracts used by REST and MCP."""

from dataclasses import dataclass, replace
from typing import Annotated, Callable, Literal

from pydantic import Field, JsonValue, StrictInt, StrictStr, model_validator

from .access import Action, AuthenticationError, Principal, require, require_operation
from .artifact import Digest
from .evaluate import EvaluationRequest
from .execution import ExecutionService
from .execution_model import ExecutionRecord, ExecutionRequest, OperationInspection
from .lifecycle import BuildRecord, Publication, ShopService, SourceRevision
from .model import Id, Model, Observation
from .program_model import ReadRun, RunPython, RunRecord
from .programs import ProgramService
from .project import SourceProject
from .published import Collection, ObservationSnapshot, PublishedService
from .query import VariableQuery, VariableResult
from .reference import sdk_reference
from .request_limits import RequestLimiter
from .responses import CatalogView, ItemView, PageView, PublicEvaluation


class Empty(Model):
    pass


class ShopInput(Model):
    shop_id: Id


class DiscoverShop(ShopInput):
    action: Literal["catalog", "page", "inspect", "evaluate", "variables"] = "catalog"
    arguments: dict[str, JsonValue] = Field(default_factory=dict, max_length=100)


class PageInput(ShopInput):
    collection: Collection
    limit: Annotated[StrictInt, Field(ge=1, le=100)] = 50
    cursor: Annotated[StrictStr, Field(max_length=2048)] | None = None
    query: Annotated[StrictStr, Field(max_length=100)] = ""


class ItemInput(ShopInput):
    collection: Collection
    item_id: Id


class EvaluationInput(ShopInput):
    query: EvaluationRequest


class VariablesInput(ShopInput):
    query: VariableQuery


class SourceRead(ShopInput):
    revision: Digest | None = None


class OperationInput(ShopInput):
    operation_id: Id


class ExecutionRead(OperationInput):
    execution_id: Digest


class SourceWrite(Model):
    project: SourceProject
    expected_head: Digest | None


class BuildCreate(ShopInput):
    revision: Digest
    request_key: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")]


class BuildRead(ShopInput):
    build_id: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{32}$")]


class PublicationChange(ShopInput):
    action: Literal["publish", "rollback", "withdraw"]
    build_id: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{32}$")] | None = None
    expected_generation: Annotated[StrictInt, Field(ge=0)]

    @model_validator(mode="after")
    def target(self):
        if (self.action == "withdraw") != (self.build_id is None):
            raise ValueError("publish/rollback require a build; withdrawal requires no build")
        return self


class AuditRead(ShopInput):
    after: Annotated[StrictInt, Field(ge=0)] = 0
    limit: Annotated[StrictInt, Field(ge=1, le=100)] = 100


class ObservationsWrite(ShopInput):
    expected_revision: Digest | None
    observations: Annotated[tuple[Observation, ...], Field(max_length=1000)]


@dataclass(frozen=True)
class Operation:
    input: type[Model]
    description: str
    handler: Callable
    authenticated: bool = False
    read_only: bool = True
    output: type[Model] | None = None
    mutation_action: Action | None = None


class Operations:
    def __init__(self, service: ShopService, published: PublishedService | None = None):
        self.service = service
        self.limiter = RequestLimiter(service.repository)
        self.published = published or PublishedService(service)
        self.executions = ExecutionService(service.repository)
        self.programs = ProgramService(service.repository)
        p = self.published
        s = self.service
        self.specs = {
            "code_run": Operation(RunPython, "Run Python with scoped SDK calls in an isolated container. Retry the same request key to inspect the same run.",
                                  lambda who, r: self.programs.submit(who, r), True, False, mutation_action="code:run"),
            "code_read": Operation(ReadRun, "Inspect a Python run, its result, and its service-call journal.",
                                   lambda who, r: self.programs.read(who, r.shop_id, r.run_id), True),
            "code_cancel": Operation(ReadRun, "Stop Python execution; previously dispatched service calls may have committed.",
                                     lambda who, r: self.programs.cancel(who, r.shop_id, r.run_id), True, False, mutation_action="code:cancel"),
            "operation_inspect": Operation(
                OperationInput,
                "Inspect an authorized published call contract and its binding status.",
                lambda who, r: self.executions.inspect_operation(who, r.shop_id, r.operation_id),
                True,
            ),
            "operation_submit": Operation(
                ExecutionRequest,
                "Submit a published shop operation in an explicit simulation or live mode. Reuse the request key only for retries of this exact request.",
                lambda who, r: self.executions.submit(who, r),
                True,
                False,
                mutation_action="operation:execute",
            ),
            "operation_execution_read": Operation(
                ExecutionRead,
                "Read a durable operation outcome; indeterminate does not mean no effect occurred.",
                lambda who, r: self.executions.read(who, r.shop_id, r.operation_id, r.execution_id),
                True,
            ),
            "operation_execution_cancel": Operation(
                ExecutionRead,
                "Request cancellation. An already dispatched effect can still complete; inspect the resulting state.",
                lambda who, r: self.executions.cancel(
                    who, r.shop_id, r.operation_id, r.execution_id
                ),
                True,
                False,
                mutation_action="operation:cancel",
            ),
            "identity_read": Operation(
                Empty,
                "Read your verified identity and effective shop grants.",
                lambda who, r: who,
                True,
                output=Principal,
            ),
            "sdk_reference": Operation(
                Empty,
                "Read the Python authoring schemas and contracts.",
                lambda who, r: sdk_reference(),
            ),
            "shop_catalog": Operation(
                ShopInput,
                "Read a shop's explicitly published catalog.",
                lambda who, r: p.catalog(r.shop_id),
            ),
            "shop_list": Operation(
                PageInput,
                "Page through published shop entities, with optional text filtering.",
                lambda who, r: p.page(
                    r.shop_id, r.collection, limit=r.limit, cursor=r.cursor, query=r.query
                ),
            ),
            "shop_inspect": Operation(
                ItemInput,
                "Inspect one published shop entity.",
                lambda who, r: p.item(r.shop_id, r.collection, r.item_id),
            ),
            "shop_evaluate": Operation(
                EvaluationInput,
                "Evaluate a published capability, workflow, or alternative offer at the service's current time.",
                lambda who, r: p.evaluation(r.shop_id, r.query),
            ),
            "shop_variables": Operation(
                VariablesInput,
                "Query public shop variables and their applicability in context.",
                lambda who, r: p.variables(r.shop_id, r.query),
            ),
            "shop_evaluate_private": Operation(
                EvaluationInput,
                "Read a detailed evaluation trace with authorized private dependencies.",
                lambda who, r: p.evaluation(r.shop_id, r.query, principal=who, private=True),
                True,
            ),
            "shop_variables_private": Operation(
                VariablesInput,
                "Query authorized private shop variables.",
                lambda who, r: p.variables(r.shop_id, r.query, principal=who, private=True),
                True,
            ),
            "source_read": Operation(
                SourceRead,
                "Read private source at its head or an exact revision.",
                lambda who, r: s.get_source(who, r.shop_id, r.revision),
                True,
            ),
            "source_save": Operation(
                SourceWrite,
                "Save a Python project with a source-head guard; source is not executed.",
                lambda who, r: s.save_source(who, r.project, expected_head=r.expected_head),
                True,
                False,
                mutation_action="source:write",
            ),
            "build_request": Operation(
                BuildCreate,
                "Queue an exact source revision; reuse the request key to safely retry.",
                lambda who, r: s.request_build(
                    who, r.shop_id, r.revision, request_key=r.request_key
                ),
                True,
                False,
                mutation_action="build:run",
            ),
            "build_inspect": Operation(
                BuildRead,
                "Read a build's status, diagnostics, tests, and private artifact.",
                lambda who, r: s.get_build(who, r.shop_id, r.build_id),
                True,
            ),
            "publication_read": Operation(
                ShopInput,
                "Read the current publication generation and selected build.",
                lambda who, r: s.get_publication(who, r.shop_id),
                True,
            ),
            "publication_change": Operation(
                PublicationChange,
                "Publish, roll back, or withdraw using the current publication generation.",
                self._publication,
                True,
                False,
                mutation_action="publication:write",
            ),
            "audit_read": Operation(
                AuditRead,
                "Read a bounded page of the shop's audit events.",
                lambda who, r: {"events": s.audit(who, r.shop_id, after=r.after, limit=r.limit)},
                True,
            ),
            "evidence_read": Operation(
                ShopInput,
                "Read authorized evidence for the published shop.",
                lambda who, r: p.read_evidence(who, r.shop_id),
                True,
            ),
            "observations_read": Operation(
                ShopInput,
                "Read the authorized current observation snapshot.",
                lambda who, r: p.read_observations(who, r.shop_id),
                True,
            ),
            "observations_write": Operation(
                ObservationsWrite,
                "Replace a shop observation snapshot with a revision guard.",
                lambda who, r: p.write_observations(
                    who, r.shop_id, r.observations, expected_revision=r.expected_revision
                ),
                True,
                False,
                mutation_action="observations:write",
            ),
        }
        outputs = {
            "code_run": RunRecord,
            "code_read": RunRecord,
            "code_cancel": RunRecord,
            "operation_inspect": OperationInspection,
            "operation_submit": ExecutionRecord,
            "operation_execution_read": ExecutionRecord,
            "operation_execution_cancel": ExecutionRecord,
            "identity_read": Principal,
            "shop_catalog": CatalogView,
            "shop_list": PageView,
            "shop_inspect": ItemView,
            "shop_evaluate": PublicEvaluation,
            "shop_variables": VariableResult,
            "shop_variables_private": VariableResult,
            "source_read": SourceRevision,
            "source_save": SourceRevision,
            "build_request": BuildRecord,
            "build_inspect": BuildRecord,
            "publication_read": Publication,
            "publication_change": Publication,
            "observations_read": ObservationSnapshot,
            "observations_write": ObservationSnapshot,
        }
        self.specs = {
            name: replace(spec, output=outputs.get(name)) for name, spec in self.specs.items()
        }
        if any(not spec.read_only and spec.mutation_action is None for spec in self.specs.values()):
            raise ValueError("mutating operations require an explicit action grant")

    def _publication(self, principal, request):
        arguments = (principal, request.shop_id)
        if request.action != "withdraw":
            arguments += (request.build_id,)
        return getattr(self.service, request.action)(
            *arguments, expected_generation=request.expected_generation
        )

    def discover(self, request):
        request = DiscoverShop.model_validate(request)
        operation = {"catalog": "shop_catalog", "page": "shop_list", "inspect": "shop_inspect",
                     "evaluate": "shop_evaluate", "variables": "shop_variables"}[request.action]
        return self.call(operation, None, {**request.arguments, "shop_id": request.shop_id})

    def call(self, name, principal, request) -> dict:
        spec = self.specs[name]
        if spec.authenticated and principal is None:
            raise AuthenticationError("authentication required")
        request = spec.input.model_validate(request)
        if not spec.read_only:
            shop = request.project.shop_id if isinstance(request, SourceWrite) else request.shop_id
            who = (
                require_operation(principal, shop, request.operation_id, spec.mutation_action)
                if isinstance(request, (ExecutionRequest, ExecutionRead))
                else require(principal, shop, spec.mutation_action)
            )
            self.limiter.mutation(who.subject, shop)
        result = spec.handler(principal, request)
        if spec.output:
            result = spec.output.model_validate(result)
        return result.model_dump(mode="json") if isinstance(result, Model) else result
