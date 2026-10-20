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

from .access import Forbidden
from .artifact import canonical_json, digest
from .auth import AuthenticationError, IdentityUnavailable, ServerSettings, TokenVerifier
from .cache import DataBusy
from .lifecycle import (
    BuildRecord,
    Conflict,
    IntegrityError,
    NotFound,
    Publication,
    Repository,
    ShopService,
    SourceRevision,
)
from .operations import (
    AuditRead,
    BuildCreate,
    BuildRead,
    Empty,
    EvaluationInput,
    ItemInput,
    ObservationsWrite,
    Operations,
    PageInput,
    PublicationChange,
    ShopInput,
    SourceRead,
    SourceWrite,
    VariablesInput,
)
from .project import unique_json
from .published import Collection, ObservationSnapshot
from .query import VariableResult
from .quotas import QuotaExceeded
from .responses import CatalogView, ItemView, PageView, PublicEvaluation

MAX_REQUEST_BYTES = 3 * 1024 * 1024


def problem(error: Exception) -> tuple[int, dict]:
    if isinstance(error, IntegrityError):
        status, code, message = 500, "internal_error", "request could not be completed"
    elif isinstance(error, AuthenticationError):
        status, code, message = 401, "authentication_required", str(error)
    elif isinstance(error, Forbidden):
        status, code, message = 403, "permission_denied", "permission denied"
    elif isinstance(error, NotFound):
        status, code, message = 404, "not_found", str(error)
    elif isinstance(error, Conflict):
        status, code, message = 409, "revision_conflict", str(error)
    elif isinstance(error, IdentityUnavailable):
        status, code, message = 503, "identity_unavailable", "identity verification is unavailable"
    elif isinstance(error, DataBusy):
        status, code, message = 503, "data_busy", str(error)
    elif isinstance(error, QuotaExceeded):
        return 429, {
            "error": {
                "code": "quota_exceeded",
                "message": str(error),
                "quota": error.quota,
                "retry_after": error.retry_after,
            }
        }
    elif isinstance(error, (ValidationError, RequestValidationError)):
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
    elif isinstance(error, ValueError):
        status, code, message = 422, "invalid_request", str(error)
    else:
        status, code, message = 500, "internal_error", "request could not be completed"
    return status, {"error": {"code": code, "message": message}}


def challenge(settings: ServerSettings) -> str:
    return f'Bearer resource_metadata="{settings.metadata_url}"'


class Boundary:
    """Bound requests before either protocol parser; verify identity once per HTTP request."""

    def __init__(self, app, settings, verifier, operations):
        self.app, self.settings, self.verifier, self.operations = (
            app,
            settings,
            verifier,
            operations,
        )
        self.host = urlsplit(settings.resource_url).netloc
        self.origins = {settings.base_url, *settings.allowed_origins}
        self.active = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {}
        for key, value in scope["headers"]:
            name = key.decode().lower()
            if name in headers and name in {"authorization", "host", "content-length", "origin"}:
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
        if self.active >= 32:
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
            scope["chipload.principal"] = principal
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
                if length > MAX_REQUEST_BYTES:
                    return await JSONResponse({"error": {"code": "request_too_large"}}, 413)(
                        scope, receive, send
                    )
            async with asyncio.timeout(10):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    data.extend(message.get("body", b""))
                    if len(data) > MAX_REQUEST_BYTES:
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
                    spec = self.operations.specs.get(name) if isinstance(name, str) else None
                    if spec and spec.authenticated and principal is None:
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
        except (AuthenticationError, IdentityUnavailable) as error:
            status, body = problem(error)
            await JSONResponse(
                body,
                status,
                headers={
                    "Cache-Control": "no-store",
                    "WWW-Authenticate": challenge(self.settings),
                },
            )(scope, receive, send)
        except TimeoutError:
            await JSONResponse({"error": {"code": "request_timeout"}}, 408)(scope, receive, send)
        finally:
            self.active -= 1


def make_mcp(operations: Operations, settings: ServerSettings):
    mcp = MCPServer(
        "Chipload",
        version="0.2.0.dev0",
        instructions="Discover published shop capabilities. Authoring tools require shop grants. "
        "Source, builds, and publication are distinct; simulation is not physical qualification.",
    )
    for name, spec in operations.specs.items():

        def register(name, spec):
            async def invoke(request, ctx):
                principal = ctx.request_context.request.scope.get("chipload.principal")
                try:
                    return await run_in_threadpool(operations.call, name, principal, request)
                except (
                    AuthenticationError,
                    Forbidden,
                    NotFound,
                    Conflict,
                    ValueError,
                    DataBusy,
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
                name=name,
                description=spec.description,
                structured_output=True,
                annotations=ToolAnnotations(
                    read_only_hint=spec.read_only,
                    destructive_hint=not spec.read_only,
                    idempotent_hint=spec.read_only or name == "build_request",
                    open_world_hint=False,
                ),
            )

        register(name, spec)

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
    operations = operations or Operations(
        ShopService(Repository(settings.database, cache_bytes=settings.data_cache_bytes))
    )
    owned_verifier = verifier is None
    verifier = verifier or TokenVerifier(settings)
    mcp, mcp_http = make_mcp(operations, settings)

    @asynccontextmanager
    async def lifespan(app):
        async with mcp_http.router.lifespan_context(mcp_http):
            yield
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
    app.add_middleware(Boundary, settings=settings, verifier=verifier, operations=operations)

    async def handle_error(request, error):
        status, body = problem(error)
        headers = {"WWW-Authenticate": challenge(settings)} if status == 401 else {}
        if isinstance(error, QuotaExceeded) and error.retry_after is not None:
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
        Exception,
    ):
        app.add_exception_handler(kind, handle_error)

    async def call(name, request, model):
        return await run_in_threadpool(
            operations.call, name, request.scope.get("chipload.principal"), model
        )

    def conditional(request, result):
        tag = f'"{digest(canonical_json(result))}"'
        headers = {"ETag": tag, "Cache-Control": "private, no-cache"}
        if request.headers.get("if-none-match") == tag:
            return Response(status_code=304, headers=headers)
        return JSONResponse(result, headers=headers)

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

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

    @app.get("/v1/shops/{shop_id}/catalog", response_model=CatalogView)
    async def catalog(shop_id: str, request: Request):
        return conditional(request, await call("shop_catalog", request, ShopInput(shop_id=shop_id)))

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

    @app.put("/v1/shops/{shop_id}/observations", response_model=ObservationSnapshot)
    async def observation_write(shop_id: str, body: ObservationsWrite, request: Request):
        if body.shop_id != shop_id:
            raise ValueError("body and path shop IDs must match")
        return await call("observations_write", request, body)

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

    app.openapi = openapi
    app.mount("/", mcp_http)
    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    settings = ServerSettings.model_validate_json(args.config.read_bytes())
    address = urlsplit(settings.resource_url)
    uvicorn.run(
        create_app(settings),
        host="127.0.0.1",
        port=address.port or 8088,
        proxy_headers=False,
        access_log=False,
        limit_concurrency=64,
        timeout_keep_alive=5,
        h11_max_incomplete_event_size=32 * 1024,
    )


if __name__ == "__main__":
    main()
