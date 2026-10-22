"""REST and Streamable HTTP MCP over the same authorized application operations."""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from .access import Forbidden, require
from .artifact import canonical_json, digest
from .assessment_model import AssessmentRead, AssessmentRecord, AssessShop
from .auth import AuthenticationError, IdentityUnavailable, ServerSettings, TokenVerifier
from .cache import DataBusy
from .evidence import EvidenceReference, EvidenceRevocation, RevokeEvidence
from .execution_model import ExecutionRecord, ExecutionRequest, OperationInspection
from .lifecycle import (
    BuildRecord,
    Conflict,
    NotFound,
    Publication,
    Repository,
    ShopService,
    SourceRevision,
)
from .network_model import NetworkClaim, NetworkComplete, NetworkRead, NetworkRecord, NetworkSubmit
from .operations import (
    AuditRead,
    BuildCreate,
    BuildRead,
    DiscoverShop,
    Empty,
    EvaluationInput,
    ExecutionRead,
    ItemInput,
    ListingEvaluationInput,
    ObservationsWrite,
    Operation,
    OperationInput,
    Operations,
    PageInput,
    PeerQueryInput,
    PublicationChange,
    ShopInput,
    SourceRead,
    SourceWrite,
    VariablesInput,
)
from .peer_model import MAX_PEER_REQUEST_BYTES, PeerDescriptor, PeerRequest, PeerResponse
from .process_operations import ProcessOperations
from .program_model import ReadRun, RunPython, RunRecord
from .project import unique_json
from .published import Collection, ObservationSnapshot
from .query import VariableResult
from .quotas import QuotaExceeded
from .request_limits import RateLimited, RequestCapacity, request_peer
from .responses import CatalogView, ItemView, ListingEvaluation, PageView, PublicEvaluation
from .service_errors import problem as service_problem
from .service_pool import ServiceFailure

MAX_REQUEST_BYTES = 3 * 1024 * 1024


def problem(error: Exception) -> tuple[int, dict]:
    if isinstance(error, RequestValidationError):
        return 422, {
            "error": {
                "code": "invalid_request",
                "message": "request validation failed",
                "fields": [
                    {"path": list(item["loc"]), "message": item["msg"]}
                    for item in error.errors()[:100]
                ],
            }
        }
    return service_problem(error)


def challenge(settings: ServerSettings) -> str:
    return f'Bearer resource_metadata="{settings.metadata_url}"'


class Boundary:
    """Bound requests before either protocol parser; verify identity once per HTTP request."""

    def __init__(self, app, settings, verifier, operations, *, max_active=32,
                 max_request_bytes=MAX_REQUEST_BYTES, authorize=None):
        self.app, self.settings, self.verifier, self.operations = (
            app,
            settings,
            verifier,
            operations,
        )
        self.host = urlsplit(settings.resource_url).netloc
        self.origins = {settings.base_url, *settings.allowed_origins}
        self.active = 0
        self.max_active = max_active
        self.max_request_bytes = max_request_bytes
        self.authorize = authorize

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {}
        for key, value in scope["headers"]:
            name = key.decode().lower()
            if name in headers and name in {
                "authorization",
                "host",
                "content-length",
                "origin",
                "x-forwarded-for",
            }:
                return await JSONResponse({"error": {"code": "duplicate_header"}}, 400)(
                    scope, receive, send
                )
            headers[name] = value.decode("latin-1")
        if headers.get("host") != self.host:
            return await JSONResponse({"error": {"code": "invalid_host"}}, 400)(
                scope, receive, send
            )
        origin = headers.get("origin")
        if origin is not None and origin not in self.origins:
            return await JSONResponse({"error": {"code": "invalid_origin"}}, 403)(
                scope, receive, send
            )
        if self.active >= self.max_active:
            return await JSONResponse(
                {"error": {"code": "busy"}}, 503, headers={"Retry-After": "1"}
            )(scope, receive, send)
        if any(
            k in parse_qs(scope["query_string"].decode("latin-1"))
            for k in ("access_token", "authorization")
        ):
            return await JSONResponse({"error": {"code": "token_in_query"}}, 400)(
                scope, receive, send
            )
        self.active += 1
        try:
            try:
                client = request_peer(
                    scope.get("client"),
                    headers.get("x-forwarded-for"),
                    self.settings.trusted_proxy_ips,
                )
            except ValueError:
                return await JSONResponse({"error": {"code": "invalid_proxy_address"}}, 400)(
                    scope, receive, send
                )
            await run_in_threadpool(self.operations.limiter.ingress, client)
            if scope["method"] == "OPTIONS":
                response = Response(
                    status_code=204,
                    headers={
                        "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
                        "Access-Control-Allow-Headers": "Authorization, Content-Type, If-None-Match, MCP-Protocol-Version, Mcp-Method, Mcp-Name",
                        "Access-Control-Allow-Origin": origin or self.settings.base_url,
                        "Vary": "Origin",
                        "Cache-Control": "no-store",
                    },
                )
                return await response(scope, receive, send)
            principal = None
            if "authorization" in headers:
                kind, separator, token = headers["authorization"].partition(" ")
                if kind.lower() != "bearer" or not separator or not token or " " in token:
                    raise AuthenticationError("invalid authorization header")
                principal = await run_in_threadpool(self.verifier.verify, token)
                await run_in_threadpool(self.operations.limiter.identity, principal.subject)
            scope["chipload.principal"] = principal
            if self.authorize is not None:
                await run_in_threadpool(self.authorize, scope, principal)
            data = bytearray()
            if "content-length" in headers:
                try:
                    length = int(headers["content-length"])
                except ValueError:
                    length = -1
                if length < 0:
                    return await JSONResponse({"error": {"code": "invalid_length"}}, 400)(
                        scope, receive, send
                    )
                if length > self.max_request_bytes:
                    return await JSONResponse({"error": {"code": "request_too_large"}}, 413)(
                        scope, receive, send
                    )
            async with asyncio.timeout(10):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    data.extend(message.get("body", b""))
                    if len(data) > self.max_request_bytes:
                        return await JSONResponse({"error": {"code": "request_too_large"}}, 413)(
                            scope, receive, send
                        )
                    if not message.get("more_body", False):
                        break
            payload = None
            if data:
                if headers.get("content-encoding", "identity") != "identity":
                    return await JSONResponse({"error": {"code": "unsupported_encoding"}}, 415)(
                        scope, receive, send
                    )
                if headers.get("content-type", "").split(";")[0].strip() != "application/json":
                    return await JSONResponse({"error": {"code": "json_required"}}, 415)(
                        scope, receive, send
                    )
                try:
                    payload = unique_json(bytes(data))
                except (ValueError, RecursionError):
                    return await JSONResponse({"error": {"code": "invalid_json"}}, 400)(
                        scope, receive, send
                    )
            if scope["path"] == "/mcp" and isinstance(payload, dict):
                params = payload.get("params")
                if payload.get("method") == "tools/call" and isinstance(params, dict):
                    name = params.get("name")
                    if name in ("run_python", "read_run", "cancel_run") and principal is None:
                        raise AuthenticationError("authentication required")
                if (
                    payload.get("method") == "resources/read"
                    and isinstance(params, dict)
                    and params.get("uri") == "chipload://identity/current"
                    and principal is None
                ):
                    raise AuthenticationError("authentication required")
            delivered = False

            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(data), "more_body": False}
                return await receive()

            async def secured(message):
                if message["type"] == "http.response.start":
                    fields = dict(message["headers"])
                    fields.setdefault(b"cache-control", b"no-store")
                    fields[b"x-content-type-options"] = b"nosniff"
                    fields[b"content-security-policy"] = (
                        b"default-src 'none'; frame-ancestors 'none'"
                    )
                    fields[b"vary"] = b"Authorization, Origin"
                    if origin:
                        fields[b"access-control-allow-origin"] = origin.encode()
                        fields[b"access-control-expose-headers"] = (
                            b"ETag, WWW-Authenticate, Retry-After"
                        )
                    message["headers"] = list(fields.items())
                await send(message)

            await self.app(scope, replay, secured)
        except (AuthenticationError, IdentityUnavailable, Forbidden, NotFound, RateLimited, RequestCapacity) as error:
            status, body = problem(error)
            response_headers = {"Cache-Control": "no-store", "Vary": "Authorization, Origin"}
            if isinstance(error, (AuthenticationError, IdentityUnavailable)):
                response_headers["WWW-Authenticate"] = challenge(self.settings)
            if isinstance(error, (RateLimited, RequestCapacity)):
                response_headers["Retry-After"] = str(error.retry_after)
                if scope["path"] == "/mcp":
                    # Preserve retry data in MCP clients before parsing a request ID.
                    # Positive application codes avoid MCP's reserved negative range.
                    body = {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {
                            "code": status,
                            "message": str(error),
                            "data": {"status": status, **body},
                        },
                    }
            if origin:
                response_headers["Access-Control-Allow-Origin"] = origin
                response_headers["Access-Control-Expose-Headers"] = "WWW-Authenticate, Retry-After"
            await JSONResponse(
                body,
                status,
                headers=response_headers,
            )(scope, receive, send)
        except TimeoutError:
            await JSONResponse({"error": {"code": "request_timeout"}}, 408)(scope, receive, send)
        finally:
            self.active -= 1


def make_mcp(operations: Operations, settings: ServerSettings):
    mcp = MCPServer(
        "Chipload",
        version="0.2.0.dev0",
        instructions="Use discover_shop for public capabilities. Author and query shops with Python through run_python. "
        "Read chipload://sdk/python for the client API and chipload://identity/current for your grants. "
        "Code runs in an isolated container with client and inputs available; assign result to return JSON. "
        "Retry the same request key to inspect the same run; failed or cancelled code may already have committed service calls. "
        "Build and inspect shop source before publishing. Simulation is not physical qualification.",
    )
    tools = {
        "run_python": ("code_run", operations.specs["code_run"]),
        "read_run": ("code_read", operations.specs["code_read"]),
        "cancel_run": ("code_cancel", operations.specs["code_cancel"]),
        "discover_shop": (
            None,
            Operation(
                DiscoverShop,
                "Discover a public shop catalog, page or item, or evaluate capabilities and business variables. See chipload://sdk/reference for query schemas.",
                lambda who, request: operations.discover(request),
            ),
        ),
    }
    for tool_name, (name, spec) in tools.items():

        def register(tool_name, name, spec):
            async def invoke(request, ctx):
                principal = ctx.request_context.request.scope.get("chipload.principal")
                try:
                    if name is None:
                        return await run_in_threadpool(operations.discover, request)
                    return await run_in_threadpool(operations.call, name, principal, request)
                except (
                    AuthenticationError,
                    Forbidden,
                    NotFound,
                    Conflict,
                    ValueError,
                    DataBusy,
                    RequestCapacity,
                    ServiceFailure,
                ) as error:
                    status, body = problem(error)
                    result = {"status": status, **body}
                    return CallToolResult(
                        is_error=True,
                        structured_content=result,
                        content=[TextContent(type="text", text=json.dumps(result))],
                    )

            invoke.__annotations__ = {
                "request": spec.input,
                "ctx": Context,
                "return": spec.output or dict[str, Any],
            }
            mcp.add_tool(
                invoke,
                name=tool_name,
                description=spec.description,
                structured_output=True,
                annotations=ToolAnnotations(
                    read_only_hint=spec.read_only,
                    destructive_hint=not spec.read_only,
                    idempotent_hint=True,
                    open_world_hint=tool_name in {"run_python", "cancel_run"},
                ),
            )

        register(tool_name, name, spec)

    @mcp.resource("chipload://sdk/python", mime_type="application/json")
    async def python_reference():
        from .client import python_reference

        return json.dumps(python_reference())

    @mcp.resource("chipload://identity/{view}", mime_type="application/json")
    async def identity_reference(view: str, ctx: Context):
        if view != "current":
            raise ValueError("unknown identity resource")
        principal = ctx.request_context.request.scope.get("chipload.principal")
        return json.dumps(await run_in_threadpool(operations.call, "identity_read", principal, {}))

    @mcp.resource("chipload://sdk/reference", mime_type="application/json")
    async def reference():
        return json.dumps(await run_in_threadpool(operations.call, "sdk_reference", None, {}))

    @mcp.resource("chipload://shops/{shop_id}/catalog", mime_type="application/json")
    async def catalog(shop_id: str):
        return json.dumps(
            await run_in_threadpool(operations.call, "shop_catalog", None, {"shop_id": shop_id})
        )

    http = mcp.streamable_http_app(
        json_response=True,
        stateless_http=True,
        max_request_body_size=MAX_REQUEST_BYTES,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[urlsplit(settings.resource_url).netloc],
            allowed_origins=[settings.base_url, *settings.allowed_origins],
        ),
    )
    return mcp, http


def create_app(
    settings: ServerSettings,
    *,
    operations: Operations | None = None,
    verifier: TokenVerifier | None = None,
) -> FastAPI:
    owned_operations = operations is None
    operations = operations or ProcessOperations(
        ShopService(Repository(settings.database, cache_bytes=0)),
        cache_bytes=settings.data_cache_bytes,
    )
    owned_verifier = verifier is None
    verifier = verifier or TokenVerifier(settings)
    mcp, mcp_http = make_mcp(operations, settings)

    @asynccontextmanager
    async def lifespan(app):
        try:
            if owned_operations:
                await run_in_threadpool(operations.start)
            async with mcp_http.router.lifespan_context(mcp_http):
                yield
        finally:
            try:
                if owned_operations:
                    await run_in_threadpool(operations.close)
            finally:
                if owned_verifier:
                    verifier.close()

    app = FastAPI(
        title="Chipload",
        version="0.2.0.dev0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        description="Software-defined shop discovery and authoring.",
    )
    app.state.operations = operations
    app.state.mcp = mcp
    def authorize_network(scope, principal):
        parts = scope.get("path", "").split("/")
        if len(parts) >= 6 and parts[1:3] == ["v1", "shops"] and parts[4] == "network":
            if principal is None:
                raise AuthenticationError("authentication required")
            require(principal, parts[3], "network:dispatch" if parts[5] in {"claim", "complete"} else "network:query")

    app.add_middleware(Boundary, settings=settings, verifier=verifier, operations=operations,
                       authorize=authorize_network)

    async def handle_error(request, error):
        status, body = problem(error)
        headers = {"WWW-Authenticate": challenge(settings)} if status == 401 else {}
        if (
            isinstance(error, (QuotaExceeded, RateLimited, RequestCapacity, ServiceFailure))
            and error.retry_after is not None
        ):
            headers["Retry-After"] = str(error.retry_after)
        return JSONResponse(body, status, headers=headers)

    for kind in (
        AuthenticationError,
        Forbidden,
        NotFound,
        Conflict,
        ValueError,
        ValidationError,
        RequestValidationError,
        DataBusy,
        RequestCapacity,
        ServiceFailure,
        Exception,
    ):
        app.add_exception_handler(kind, handle_error)

    async def call(name, request, model, *, as_response=True):
        result = await run_in_threadpool(
            operations.call, name, request.scope.get("chipload.principal"), model
        )
        # The service worker validates its output contract. Avoid rebuilding a
        # large artifact model a second time in the HTTP supervisor.
        if as_response:
            status = 202 if name in {"build_request", "operation_submit", "code_run", "assessment_submit", "network_submit"} else 200
            return JSONResponse(result, status_code=status)
        return result

    def conditional(request, result):
        tag = f'"{digest(canonical_json(result))}"'
        headers = {"ETag": tag, "Cache-Control": "private, no-cache"}
        if request.headers.get("if-none-match") == tag:
            return Response(status_code=304, headers=headers)
        return JSONResponse(result, headers=headers)

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/.well-known/chipload", response_model=PeerDescriptor)
    def peer_descriptor():
        if settings.instance_id is None:
            raise NotFound("instance federation is not enabled")
        return JSONResponse(PeerDescriptor(instance_id=settings.instance_id).model_dump(mode="json"),
                            headers={"Cache-Control": "no-store"})

    @app.post("/v1/peer/query", response_model=PeerResponse)
    async def peer_query(body: PeerRequest, request: Request):
        from datetime import datetime, timezone

        if settings.instance_id is None or body.instance_id != settings.instance_id:
            raise NotFound("instance not found")
        if len(canonical_json(body)) > MAX_PEER_REQUEST_BYTES:
            raise ValueError("peer request exceeds its byte limit")
        remaining = (body.deadline - datetime.now(timezone.utc)).total_seconds()
        if not 0 < remaining <= 30:
            raise ValueError("peer request requires a future deadline within 30 seconds")
        result = await call("peer_query", request, PeerQueryInput(query=body.query), as_response=False)
        if datetime.now(timezone.utc) >= body.deadline:
            return JSONResponse({"error": {"code": "peer_deadline", "message": "Peer query deadline expired."}}, 408)
        envelope = PeerResponse(instance_id=settings.instance_id, request_id=body.request_id, result=result)
        return JSONResponse(envelope.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.get("/.well-known/oauth-protected-resource")
    @app.get("/.well-known/oauth-protected-resource/mcp")
    def resource_metadata():
        return settings.protected_resource_metadata()

    @app.get("/v1/sdk")
    async def sdk(request: Request):
        return await call("sdk_reference", request, Empty())

    @app.get("/v1/identity")
    async def identity(request: Request):
        return await call("identity_read", request, Empty())

    @app.post("/v1/shops/{shop_id}/network/queries", status_code=202, response_model=NetworkRecord)
    async def network_submit(shop_id: str, body: NetworkSubmit, request: Request):
        if body.shop_id != shop_id:
            raise ValueError("body and path shop IDs must match")
        return await call("network_submit", request, body)

    @app.get("/v1/shops/{shop_id}/network/queries/{job_id}", response_model=NetworkRecord)
    async def network_read(shop_id: str, job_id: str, request: Request):
        return await call("network_read", request, NetworkRead(shop_id=shop_id, job_id=job_id))

    @app.post("/v1/shops/{shop_id}/network/queries/{job_id}/cancel", response_model=NetworkRecord)
    async def network_cancel(shop_id: str, job_id: str, request: Request):
        return await call("network_cancel", request, NetworkRead(shop_id=shop_id, job_id=job_id))

    @app.post("/v1/shops/{shop_id}/network/claim")
    async def network_claim(shop_id: str, body: NetworkClaim, request: Request):
        if body.shop_id != shop_id:
            raise ValueError("body and path shop IDs must match")
        return await call("network_claim", request, body)

    @app.post("/v1/shops/{shop_id}/network/complete")
    async def network_complete(shop_id: str, body: NetworkComplete, request: Request):
        if body.shop_id != shop_id:
            raise ValueError("body and path shop IDs must match")
        return await call("network_complete", request, body)

    @app.post("/v1/shops/{shop_id}/runs", status_code=202, response_model=RunRecord)
    async def code_run(shop_id: str, body: RunPython, request: Request):
        if body.shop_id != shop_id:
            raise ValueError("body and path shop IDs must match")
        return await call("code_run", request, body)

    @app.get("/v1/shops/{shop_id}/runs/{run_id}", response_model=RunRecord)
    async def code_read(shop_id: str, run_id: str, request: Request):
        return await call("code_read", request, ReadRun(shop_id=shop_id, run_id=run_id))

    @app.post("/v1/shops/{shop_id}/runs/{run_id}/cancel", response_model=RunRecord)
    async def code_cancel(shop_id: str, run_id: str, request: Request):
        return await call("code_cancel", request, ReadRun(shop_id=shop_id, run_id=run_id))

    @app.get("/v1/shops/{shop_id}/catalog", response_model=CatalogView)
    async def catalog(shop_id: str, request: Request):
        return conditional(
            request,
            await call("shop_catalog", request, ShopInput(shop_id=shop_id), as_response=False),
        )

    @app.post("/v1/shops/{shop_id}/assessments", status_code=202, response_model=AssessmentRecord)
    async def assessment_submit(shop_id: str, body: AssessShop, request: Request):
        if body.shop_id != shop_id:
            raise ValueError("body and path shop IDs must match")
        return await call("assessment_submit", request, body)

    @app.get("/v1/shops/{shop_id}/assessments/{assessment_id}", response_model=AssessmentRecord)
    async def assessment_read(shop_id: str, assessment_id: str, request: Request):
        return await call("assessment_read", request, AssessmentRead(shop_id=shop_id, assessment_id=assessment_id))

    @app.post("/v1/shops/{shop_id}/assessments/{assessment_id}/cancel", response_model=AssessmentRecord)
    async def assessment_cancel(shop_id: str, assessment_id: str, request: Request):
        return await call("assessment_cancel", request, AssessmentRead(shop_id=shop_id, assessment_id=assessment_id))

    @app.post("/v1/shops/{shop_id}/evaluate", response_model=PublicEvaluation)
    async def evaluation(shop_id: str, body: EvaluationInput, request: Request):
        if body.shop_id != shop_id:
            raise ValueError("body and path shop IDs must match")
        return await call("shop_evaluate", request, body)

    @app.post("/v1/shops/{shop_id}/variables/query", response_model=VariableResult)
    async def variables(shop_id: str, body: VariablesInput, request: Request):
        if body.shop_id != shop_id:
            raise ValueError("body and path shop IDs must match")
        return await call("shop_variables", request, body)

    @app.post("/v1/shops/{shop_id}/listings/{listing_id}/evaluate", response_model=ListingEvaluation)
    async def listing_evaluation(shop_id: str, listing_id: str, body: ListingEvaluationInput, request: Request):
        if body.shop_id != shop_id or body.listing_id != listing_id:
            raise ValueError("body and path identities must match")
        return await call("listing_evaluate", request, body)

    @app.post("/v1/shops/{shop_id}/private/evaluate")
    async def private_evaluation(shop_id: str, body: EvaluationInput, request: Request):
        if body.shop_id != shop_id:
            raise ValueError("body and path shop IDs must match")
        return await call("shop_evaluate_private", request, body)

    @app.post("/v1/shops/{shop_id}/private/variables/query")
    async def private_variables(shop_id: str, body: VariablesInput, request: Request):
        if body.shop_id != shop_id:
            raise ValueError("body and path shop IDs must match")
        return await call("shop_variables_private", request, body)

    @app.get("/v1/shops/{shop_id}/source", response_model=SourceRevision)
    async def source_read(shop_id: str, request: Request, revision: str | None = None):
        return await call("source_read", request, SourceRead(shop_id=shop_id, revision=revision))

    @app.put("/v1/shops/{shop_id}/source", response_model=SourceRevision)
    async def source_save(shop_id: str, body: SourceWrite, request: Request):
        if body.project.shop_id != shop_id:
            raise ValueError("body and path shop IDs must match")
        return await call("source_save", request, body)

    @app.post("/v1/shops/{shop_id}/builds", status_code=202, response_model=BuildRecord)
    async def build_request(shop_id: str, body: BuildCreate, request: Request):
        if body.shop_id != shop_id:
            raise ValueError("body and path shop IDs must match")
        return await call("build_request", request, body)

    @app.get("/v1/shops/{shop_id}/builds/{build_id}", response_model=BuildRecord)
    async def build_inspect(shop_id: str, build_id: str, request: Request):
        return await call("build_inspect", request, BuildRead(shop_id=shop_id, build_id=build_id))

    @app.get("/v1/shops/{shop_id}/publication", response_model=Publication)
    async def publication_read(shop_id: str, request: Request):
        return await call("publication_read", request, ShopInput(shop_id=shop_id))

    @app.put("/v1/shops/{shop_id}/publication", response_model=Publication)
    async def publication_change(shop_id: str, body: PublicationChange, request: Request):
        if body.shop_id != shop_id:
            raise ValueError("body and path shop IDs must match")
        return await call("publication_change", request, body)

    @app.get("/v1/shops/{shop_id}/audit")
    async def audit(shop_id: str, request: Request, after: int = 0, limit: int = 100):
        return await call(
            "audit_read", request, AuditRead(shop_id=shop_id, after=after, limit=limit)
        )

    @app.get("/v1/shops/{shop_id}/evidence")
    async def evidence(shop_id: str, request: Request):
        return await call("evidence_read", request, ShopInput(shop_id=shop_id))

    @app.get("/v1/shops/{shop_id}/observations", response_model=ObservationSnapshot)
    async def observation_read(shop_id: str, request: Request):
        return await call("observations_read", request, ShopInput(shop_id=shop_id))

    @app.get("/v1/shops/{shop_id}/evidence/{evidence_id}/revocation", response_model=EvidenceRevocation)
    async def evidence_revocation(shop_id: str, evidence_id: str, request: Request):
        return await call("evidence_revocation_read", request,
                          EvidenceReference(shop_id=shop_id, evidence_id=evidence_id))

    @app.put("/v1/shops/{shop_id}/evidence/{evidence_id}/revocation", response_model=EvidenceRevocation)
    async def evidence_revoke(shop_id: str, evidence_id: str, body: RevokeEvidence, request: Request):
        if (body.shop_id, body.evidence_id) != (shop_id, evidence_id):
            raise ValueError("body and path evidence identities must match")
        return await call("evidence_revoke", request, body)

    @app.put("/v1/shops/{shop_id}/observations", response_model=ObservationSnapshot)
    async def observation_write(shop_id: str, body: ObservationsWrite, request: Request):
        if body.shop_id != shop_id:
            raise ValueError("body and path shop IDs must match")
        return await call("observations_write", request, body)

    @app.get(
        "/v1/shops/{shop_id}/private/operations/{operation_id}", response_model=OperationInspection
    )
    async def operation_inspect(shop_id: str, operation_id: str, request: Request):
        return await call(
            "operation_inspect", request, OperationInput(shop_id=shop_id, operation_id=operation_id)
        )

    @app.post(
        "/v1/shops/{shop_id}/operations/{operation_id}/executions",
        status_code=202,
        response_model=ExecutionRecord,
    )
    async def operation_submit(
        shop_id: str, operation_id: str, body: ExecutionRequest, request: Request
    ):
        if body.shop_id != shop_id or body.operation_id != operation_id:
            raise ValueError("body and path shop/operation IDs must match")
        return await call("operation_submit", request, body)

    @app.get(
        "/v1/shops/{shop_id}/operations/{operation_id}/executions/{execution_id}",
        response_model=ExecutionRecord,
    )
    async def operation_execution_read(
        shop_id: str, operation_id: str, execution_id: str, request: Request
    ):
        return await call(
            "operation_execution_read",
            request,
            ExecutionRead(shop_id=shop_id, operation_id=operation_id, execution_id=execution_id),
        )

    @app.post(
        "/v1/shops/{shop_id}/operations/{operation_id}/executions/{execution_id}/cancel",
        response_model=ExecutionRecord,
    )
    async def operation_execution_cancel(
        shop_id: str, operation_id: str, execution_id: str, request: Request
    ):
        return await call(
            "operation_execution_cancel",
            request,
            ExecutionRead(shop_id=shop_id, operation_id=operation_id, execution_id=execution_id),
        )

    @app.get("/v1/shops/{shop_id}/{collection}", response_model=PageView)
    async def page(
        shop_id: str,
        collection: Collection,
        request: Request,
        limit: int = 50,
        cursor: str | None = None,
        query: str = "",
    ):
        return conditional(
            request,
            await call(
                "shop_list",
                request,
                PageInput(
                    shop_id=shop_id, collection=collection, limit=limit, cursor=cursor, query=query
                ),
                as_response=False,
            ),
        )

    @app.get("/v1/shops/{shop_id}/{collection}/{item_id}", response_model=ItemView)
    async def item(shop_id: str, collection: Collection, item_id: str, request: Request):
        return conditional(
            request,
            await call(
                "shop_inspect",
                request,
                ItemInput(shop_id=shop_id, collection=collection, item_id=item_id),
                as_response=False,
            ),
        )

    original_openapi = app.openapi

    def openapi():
        schema = original_openapi()
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["KeycloakBearer"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": f"Keycloak access token with audience {settings.resource_url}; explicit shop grants also apply.",
        }
        for path, methods in schema["paths"].items():
            protected = (
                path == "/v1/identity"
                or "/private/" in path
                or any(
                    f"/{part}" in path
                    for part in (
                        "source",
                        "builds",
                        "publication",
                        "audit",
                        "evidence",
                        "observations",
                        "executions",
                        "runs",
                    )
                )
            )
            for description in methods.values():
                if not isinstance(description, dict):
                    continue
                if protected:
                    description["security"] = [{"KeycloakBearer": []}]
                for status, meaning in (
                    ("401", "Authentication required or invalid access token."),
                    ("403", "Missing shop/action permission."),
                    ("404", "Visible resource not found."),
                    ("409", "Revision conflict; read the current revision before retrying."),
                    ("413", "Request exceeds the configured size limit."),
                ):
                    description.setdefault("responses", {}).setdefault(
                        status, {"description": meaning}
                    )
        return schema

    @app.api_route("/mcp", methods=["GET", "HEAD", "DELETE"], include_in_schema=False)
    @app.api_route("/mcp/", methods=["GET", "HEAD", "DELETE"], include_in_schema=False)
    async def no_mcp_session():
        # Stateless JSON calls need no standalone stream. The SDK's legacy GET
        # path otherwise holds a shared Boundary slot until client disconnect.
        return Response(status_code=405, headers={"Allow": "POST, OPTIONS"})

    app.openapi = openapi
    app.mount("/", mcp_http)
    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--bind", default="127.0.0.1", help="listener address; use 0.0.0.0 in a container"
    )
    parser.add_argument(
        "--port", type=int, help="listener port, independent of the public resource URL"
    )
    args = parser.parse_args()
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    settings = ServerSettings.model_validate_json(args.config.read_bytes())
    address = urlsplit(settings.resource_url)
    uvicorn.run(
        create_app(settings),
        host=args.bind,
        port=args.port or address.port or 8088,
        proxy_headers=False,
        access_log=False,
        limit_concurrency=64,
        timeout_keep_alive=5,
        timeout_graceful_shutdown=15,
        h11_max_incomplete_event_size=32 * 1024,
    )


if __name__ == "__main__":
    main()
