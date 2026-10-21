"""Optional supplier directory and public-query relay, with scoped Keycloak owners."""

import argparse
import fcntl
import os
import re
import stat
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from .access import AuthenticationError, Forbidden
from .auth import TokenVerifier
from .client import ServiceError, TransportError
from .directory import DirectoryCapacity, DirectoryStore
from .directory_model import (
    Advertisement,
    CloudSettings,
    DirectoryDescriptor,
    PublicationReceipt,
    SearchPage,
    SupplierSearch,
    Withdrawal,
)
from .lifecycle import Conflict, NotFound, Repository
from .limits import bounded_json
from .peer import PeerClient
from .peer_model import MAX_PEER_REQUEST_BYTES, PeerCatalog, PeerRequest, PeerResponse
from .relay import (
    CompleteRelay,
    PollResult,
    RelayBroker,
    RelayFailure,
    RelayPoll,
    RelaySession,
    SessionCommand,
)
from .remote_adapter import private_bytes
from .request_limits import RequestLimiter, peer_key, request_peer
from .server import Boundary, problem


@contextmanager
def serving_lock(settings):
    """A relay's in-memory sessions require one serving process per state volume."""
    path = settings.identity.database.with_name(settings.identity.database.name + ".serve-lock")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("cloud serving lock must be an owner-only regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("cloud state is already being served by another process") from None
        yield
    finally:
        os.close(descriptor)


class CloudBoundary:
    """Separate poll, reply, publication and anonymous HTTP concurrency budgets."""

    def __init__(self, app, settings, verifier, limiter):
        owners = {binding.instance_id: binding for binding in settings.bindings}

        def authorize(scope, principal):
            path = scope["path"]
            if not path.startswith("/v1/instances/"):
                return
            if principal is None:
                raise AuthenticationError("authentication required")
            found = re.fullmatch(r"/v1/instances/([0-9a-f]{32})/(publications/[a-z][a-z0-9-]{0,63}(?:/withdraw)?|relay/(?:open|poll|complete|close))", path)
            if found is None:
                raise NotFound("cloud operation not found")
            binding = owners.get(found[1])
            if (binding is None or binding.subject != principal.subject
                    or (found[2].startswith("publications/") and not binding.advertise)
                    or (found[2].startswith("relay/") and not binding.connect)):
                raise Forbidden("cloud instance grant is required")
            if ((found[2].startswith("publications/") and scope["method"] in {"PUT", "POST"})
                    or found[2] in {"relay/open", "relay/close"}):
                limiter.mutation(principal.subject, "network-" + found[1])

        def boundary(active, maximum):
            return Boundary(app, settings.identity, verifier, SimpleNamespace(limiter=limiter),
                            max_active=active, max_request_bytes=maximum, authorize=authorize)
        self.public = boundary(32, MAX_PEER_REQUEST_BYTES)
        self.publications = boundary(4, 3 * 1024**2)
        self.polls = boundary(128, 1024)
        self.replies = boundary(16, 3 * 1024**2)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        selected = self.public
        if path.startswith("/v1/instances/"):
            selected = self.publications if "/publications/" in path else self.replies
            if path.endswith("/relay/poll"):
                selected = self.polls
        return await selected(scope, receive, send)


def initialize_cloud(settings, *, clock=None):
    """Explicit fresh initialization; serving never recreates missing rate balances."""
    store = DirectoryStore(settings, create=True, clock=clock)
    Repository(settings.identity.database.with_name(settings.identity.database.name + ".limits"))
    return store


def create_cloud_app(settings: CloudSettings, *, store=None, verifier=None, broker=None):
    store = store or DirectoryStore(settings)
    if store.settings != settings:
        raise ValueError("cloud store and authorization settings must agree")
    owned_verifier = verifier is None
    verifier = verifier or TokenVerifier(settings.identity)
    if any(getattr(verifier.settings, key) != getattr(settings.identity, key)
           for key in ("resource_url", "issuer", "jwks_url", "jwks_ca_file")):
        raise ValueError("cloud identity verifier and configured trust must agree")
    broker = broker or RelayBroker()
    limits_path = settings.identity.database.with_name(settings.identity.database.name + ".limits")
    if not limits_path.exists():
        raise ValueError("cloud request-accounting storage is missing; restore the complete state")
    limiter = RequestLimiter(Repository(limits_path))

    @asynccontextmanager
    async def lifespan(app):
        with serving_lock(settings):
            try:
                yield
            finally:
                for instance, session in broker.sessions.items():
                    broker.fail_session(instance, session)
                if owned_verifier:
                    verifier.close()

    app = FastAPI(title="Chipload cloud directory and relay", version="0.2.0.dev0",
                  lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store, app.state.broker = store, broker
    app.add_middleware(CloudBoundary, settings=settings, verifier=verifier, limiter=limiter)

    async def error_handler(request, error):
        if isinstance(error, DirectoryCapacity):
            status, body = 503, {"error": {"code": "directory_capacity", "message": "Directory storage capacity is exhausted."}}
        elif isinstance(error, RelayFailure):
            status, body = error.status, {"error": {"code": error.code, "message": "Relay query could not complete."}}
        elif isinstance(error, (ServiceError, TransportError)):
            status, body = 502, {"error": {"code": "peer_unavailable", "message": "The advertised HTTPS endpoint could not be verified."}}
        else:
            status, body = problem(error)
        return JSONResponse(body, status, headers={"Cache-Control": "no-store"})

    for kind in (DirectoryCapacity, RelayFailure, ServiceError, TransportError, Conflict, NotFound,
                 AuthenticationError, Forbidden, ValueError, ValidationError, RequestValidationError, Exception):
        app.add_exception_handler(kind, error_handler)

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/.well-known/chipload-cloud", response_model=DirectoryDescriptor)
    async def descriptor():
        return {"protocol": "chipload-cloud-1", "directory_id": settings.directory_id}

    @app.get("/.well-known/oauth-protected-resource/mcp")
    async def resource_metadata():
        return settings.identity.protected_resource_metadata()

    @app.get("/v1/instances/{instance_id}/publications/{shop_id}", response_model=PublicationReceipt)
    async def publication(instance_id: str, shop_id: str):
        return await run_in_threadpool(store.head, instance_id, shop_id)

    @app.put("/v1/instances/{instance_id}/publications/{shop_id}", response_model=PublicationReceipt)
    async def advertise(instance_id: str, shop_id: str, body: Advertisement, request: Request):
        actor = request.scope["chipload.principal"].subject
        try:
            head = await run_in_threadpool(store.head, instance_id, shop_id)
        except NotFound:
            head = None
        if head is not None and head.request_id == body.request_id:
            return await run_in_threadpool(store.advertise, instance_id, shop_id, actor, body)
        if body.expected_version != (head.version if head else 0):
            raise Conflict("advertisement changed; read its current version")
        if body.origin is not None:
            current = await run_in_threadpool(
                PeerClient(body.origin, instance_id, policy=settings.egress).catalog,
                shop_id, expected_generation=body.catalog.generation,
            )
        else:
            envelope = PeerRequest(instance_id=instance_id, request_id=uuid.uuid4().hex,
                deadline=datetime.now(timezone.utc) + timedelta(seconds=20),
                query=PeerCatalog(shop_id=shop_id, expected_generation=body.catalog.generation))
            reply = await broker.submit(envelope, "advertiser:" + request.scope["chipload.principal"].subject)
            current = reply.result
        if current.revision != body.catalog.revision:
            raise Conflict("source publication changed; fetch its current catalog")
        return await run_in_threadpool(store.advertise, instance_id, shop_id, actor, body)

    @app.post("/v1/instances/{instance_id}/publications/{shop_id}/withdraw", response_model=PublicationReceipt)
    async def withdraw(instance_id: str, shop_id: str, body: Withdrawal, request: Request):
        actor = request.scope["chipload.principal"].subject
        return await run_in_threadpool(store.withdraw, instance_id, shop_id, actor, body)

    @app.post("/v1/suppliers/search", response_model=SearchPage)
    async def search(body: SupplierSearch):
        connected = broker.connected()
        return await run_in_threadpool(store.search, body, connected=connected)

    @app.post("/v1/instances/{instance_id}/relay/open", response_model=RelaySession)
    async def open_relay(instance_id: str, request: Request):
        epoch = await run_in_threadpool(store.next_epoch, instance_id, request.scope["chipload.principal"].subject)
        return broker.open(instance_id, epoch)

    @app.post("/v1/instances/{instance_id}/relay/poll", response_model=PollResult)
    async def poll(instance_id: str, body: RelayPoll):
        return await broker.poll(instance_id, body, wait_seconds=body.wait_seconds)

    @app.post("/v1/instances/{instance_id}/relay/complete")
    async def complete(instance_id: str, body: CompleteRelay):
        broker.complete(instance_id, body)
        return {"completed": True}

    @app.post("/v1/instances/{instance_id}/relay/close")
    async def close(instance_id: str, body: SessionCommand):
        broker.close(instance_id, body)
        return {"closed": True}

    @app.post("/v1/relay/query", response_model=PeerResponse)
    async def relay_query(body: PeerRequest, request: Request):
        principal = request.scope.get("chipload.principal")
        forwarded = request.headers.get("x-forwarded-for")
        peer = request_peer(request.scope.get("client"), forwarded, settings.identity.trusted_proxy_ips)
        requester = "identity:" + principal.subject if principal else "peer:" + peer_key(peer)
        return await broker.submit(body, requester)

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "serve"))
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    settings = CloudSettings.model_validate(bounded_json(private_bytes(args.config, 1024**2), max_bytes=1024**2))
    if args.action == "init":
        initialize_cloud(settings)
    else:
        uvicorn.run(create_cloud_app(settings), host=args.bind, port=args.port,
                    proxy_headers=False, access_log=False, timeout_keep_alive=5,
                    timeout_graceful_shutdown=5, workers=1)


if __name__ == "__main__":
    main()
