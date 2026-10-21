"""Python composition of Chipload services, independent of the execution transport.

The client carries requests, never principals, grants, database handles, or
operator authority. The receiving service authorizes every operation.
"""

from __future__ import annotations

import http.client
import re
import ssl
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode, urlsplit

from pydantic import TypeAdapter

from .artifact import canonical_json
from .limits import MAX_JSON_BYTES, bounded_json
from .model import Id, Model
from .project import SourceProject

MAX_RESPONSE_BYTES = MAX_JSON_BYTES
MAX_REQUEST_BYTES = 3 * 1024**2
identifier = TypeAdapter(Id)


class ServiceError(Exception):
    """A service refusal, with structured details and optional retry timing."""

    def __init__(self, status: int, detail: dict):
        self.status, self.detail = status, detail
        error = detail.get("error", {})
        self.code = (
            error.get("code", "service_error") if isinstance(error, dict) else "service_error"
        )
        self.retry_after = error.get("retry_after") if isinstance(error, dict) else None
        # Response text can contain private data. Do not put it into exception logs.
        super().__init__(f"Chipload service returned HTTP {status}")


class TransportError(Exception):
    """No usable response: a submitted mutation may already have committed."""


def json_value(value):
    if isinstance(value, Model):
        return value.model_dump(mode="json")
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    return value


def component(value):
    return identifier.validate_python(value)


def hex_component(value, length):
    if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-f]{{{length}}}", value):
        raise ValueError("invalid record identity")
    return value


def route(operation, request):
    """Fixed REST routes used by the Python client; no caller-provided URLs."""
    if operation in {"identity_read", "sdk_reference"}:
        return "GET", "/v1/identity" if operation == "identity_read" else "/v1/sdk", None
    shop = component(
        request["project"]["shop_id"] if operation == "source_save" else request["shop_id"]
    )
    base = f"/v1/shops/{shop}"
    plain = {
        "shop_catalog": ("GET", "/catalog"),
        "source_read": ("GET", "/source"),
        "source_save": ("PUT", "/source"),
        "build_request": ("POST", "/builds"),
        "publication_read": ("GET", "/publication"),
        "publication_change": ("PUT", "/publication"),
        "evidence_read": ("GET", "/evidence"),
        "observations_read": ("GET", "/observations"),
        "observations_write": ("PUT", "/observations"),
        "audit_read": ("GET", "/audit"),
        "code_run": ("POST", "/runs"),
        "shop_evaluate": ("POST", "/evaluate"),
        "shop_evaluate_private": ("POST", "/private/evaluate"),
        "shop_variables": ("POST", "/variables/query"),
        "shop_variables_private": ("POST", "/private/variables/query"),
    }
    if operation in plain:
        method, suffix = plain[operation]
    elif operation == "build_inspect":
        method, suffix = "GET", "/builds/" + hex_component(request["build_id"], 32)
    elif operation in {"evidence_revoke", "evidence_revocation_read"}:
        method = "PUT" if operation == "evidence_revoke" else "GET"
        suffix = "/evidence/" + component(request["evidence_id"]) + "/revocation"
    elif operation in {"code_read", "code_cancel"}:
        method, suffix = "GET", "/runs/" + hex_component(request["run_id"], 32)
        if operation == "code_cancel":
            method, suffix = "POST", suffix + "/cancel"
    elif operation in {"shop_list", "shop_inspect"}:
        method, suffix = "GET", "/" + component(request["collection"])
        if operation == "shop_inspect":
            suffix += "/" + component(request["item_id"])
    elif operation in {
        "operation_inspect",
        "operation_submit",
        "operation_execution_read",
        "operation_execution_cancel",
    }:
        suffix = "/operations/" + component(request["operation_id"])
        method = "GET"
        if operation == "operation_inspect":
            suffix = "/private" + suffix
        else:
            suffix += "/executions"
            if operation == "operation_submit":
                method = "POST"
            else:
                suffix += "/" + hex_component(request["execution_id"], 64)
                if operation == "operation_execution_cancel":
                    method, suffix = "POST", suffix + "/cancel"
    else:
        raise ValueError("unknown client operation")
    query_names = {
        "source_read": ("revision",),
        "shop_list": ("limit", "cursor", "query"),
        "audit_read": ("after", "limit"),
    }.get(operation, ())
    query = {name: request[name] for name in query_names if request.get(name) is not None}
    path = base + suffix + ("?" + urlencode(query) if query else "")
    body = (
        request
        if method in {"PUT", "POST"}
        and operation not in {"operation_execution_cancel", "code_cancel"}
        else None
    )
    return method, path, body


class HTTPTransport:
    def __init__(
        self,
        origin: str,
        *,
        token: str | Callable[[], str] | None = None,
        ca_file: Path | str | None = None,
        timeout: float = 10,
        allow_http_loopback: bool = False,
    ):
        parsed = urlsplit(origin)
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.port == 0
            or parsed.scheme not in {"http", "https"}
            or not re.fullmatch(r"https?://[A-Za-z0-9.\[\]:-]+/?", origin)
        ):
            raise ValueError("configure an HTTP(S) origin without paths or credentials")
        if parsed.scheme == "http" and not (
            allow_http_loopback and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        ):
            raise ValueError("HTTPS is required; development HTTP needs explicit loopback opt-in")
        if isinstance(timeout, bool) or not 0 < timeout <= 120:
            raise ValueError("timeout must be between 0 and 120 seconds")
        self.endpoint, self.token, self.timeout = parsed, token, timeout
        self.context = ssl.create_default_context(cafile=ca_file)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2

    def __call__(self, operation, request):
        method, path, body = route(operation, request)
        encoded = canonical_json(body) if body is not None else None
        if encoded is not None and len(encoded) > MAX_REQUEST_BYTES:
            raise ValueError("request exceeds the byte limit")
        token = self.token() if callable(self.token) else self.token
        if token is not None and (
            not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/-]{1,16384}=*", token)
        ):
            raise ValueError("invalid access token")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if token:
            headers["Authorization"] = "Bearer " + token
        if self.endpoint.scheme == "https":
            connection = http.client.HTTPSConnection(
                self.endpoint.hostname,
                self.endpoint.port or 443,
                timeout=self.timeout,
                context=self.context,
            )
        else:
            connection = http.client.HTTPConnection(
                self.endpoint.hostname, self.endpoint.port or 80, timeout=self.timeout
            )
        deadline = time.monotonic() + self.timeout
        try:
            connection.request(method, path, body=encoded, headers=headers)
            response = connection.getresponse()
            if response.getheader("Content-Encoding", "identity") != "identity":
                raise ValueError("encoded responses are unsupported")
            data = bytearray()
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError()
                chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - len(data)))
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise ValueError("response exceeds the byte limit")
                if not chunk:
                    break
            value = bounded_json(bytes(data), max_bytes=MAX_RESPONSE_BYTES)
            if not isinstance(value, dict):
                raise ValueError("invalid service response")
            if token and token.encode() in data:
                raise ValueError("service echoed its access token")
            if not 200 <= response.status < 300:
                raise ServiceError(response.status, value)
            return value
        except ServiceError:
            raise
        except Exception:
            # No retries or redirects: a lost mutation response is ambiguous.
            raise TransportError(
                "No usable service response; a submitted mutation may have committed"
            ) from None
        finally:
            connection.close()


class Client:
    def __init__(self, transport: Callable[[str, dict], dict]):
        self._transport = transport

    @classmethod
    def connect(cls, origin: str, **options):
        return cls(HTTPTransport(origin, **options))

    def call(self, operation: str, request: dict):
        return self._transport(operation, json_value(request))

    def shop(self, shop_id: str):
        return ShopClient(self, component(shop_id))

    def identity(self):
        return self.call("identity_read", {})

    def reference(self):
        return self.call("sdk_reference", {})


class ShopClient:
    def __init__(self, client: Client, shop_id: str):
        self.client, self.id = client, shop_id

    def _call(self, operation, **arguments):
        return self.client.call(operation, {**arguments, "shop_id": self.id})

    def run(self, code, *, request_key, files=(), inputs=None, seconds=30):
        return self._call(
            "code_run",
            code=code,
            request_key=request_key,
            files=files,
            inputs=inputs or {},
            seconds=seconds,
        )

    def run_result(self, run_id):
        return self._call("code_read", run_id=run_id)

    def cancel_run(self, run_id):
        return self._call("code_cancel", run_id=run_id)

    def source(self, revision=None):
        return self._call("source_read", revision=revision)

    def save(self, project: SourceProject | dict, *, expected_head):
        project = SourceProject.model_validate(project)
        if project.shop_id != self.id:
            raise ValueError("project belongs to another shop")
        return self.client.call("source_save", {"project": project, "expected_head": expected_head})

    def build(self, revision, *, request_key):
        return self._call("build_request", revision=revision, request_key=request_key)

    def build_result(self, build_id):
        return self._call("build_inspect", build_id=build_id)

    def publication(self):
        return self._call("publication_read")

    def publish(self, build_id, *, expected_generation):
        return self._call(
            "publication_change",
            action="publish",
            build_id=build_id,
            expected_generation=expected_generation,
        )

    def rollback(self, build_id, *, expected_generation):
        return self._call(
            "publication_change",
            action="rollback",
            build_id=build_id,
            expected_generation=expected_generation,
        )

    def withdraw(self, *, expected_generation):
        return self._call(
            "publication_change", action="withdraw", expected_generation=expected_generation
        )

    def catalog(self):
        return self._call("shop_catalog")

    def page(self, collection, *, limit=50, cursor=None, query=""):
        return self._call(
            "shop_list", collection=collection, limit=limit, cursor=cursor, query=query
        )

    def inspect(self, collection, item_id):
        return self._call("shop_inspect", collection=collection, item_id=item_id)

    def evaluate(self, query, *, private=False):
        return self._call("shop_evaluate_private" if private else "shop_evaluate", query=query)

    def variables(self, query, *, private=False):
        return self._call("shop_variables_private" if private else "shop_variables", query=query)

    def evidence(self):
        return self._call("evidence_read")

    def evidence_revocation(self, evidence_id):
        return self._call("evidence_revocation_read", evidence_id=evidence_id)

    def revoke_evidence(self, evidence_id, *, expected_model_sha256, reason):
        return self._call(
            "evidence_revoke", evidence_id=evidence_id,
            expected_model_sha256=expected_model_sha256, reason=reason,
        )

    def observations(self):
        return self._call("observations_read")

    def set_observations(self, observations, *, expected_revision):
        return self._call(
            "observations_write", observations=observations, expected_revision=expected_revision
        )

    def audit(self, *, after=0, limit=100):
        return self._call("audit_read", after=after, limit=limit)

    def operation(self, operation_id):
        return self._call("operation_inspect", operation_id=operation_id)

    def execute(self, operation_id, *, inputs, mode, expected_generation, request_key):
        return self._call(
            "operation_submit",
            operation_id=operation_id,
            inputs=inputs,
            mode=mode,
            expected_generation=expected_generation,
            request_key=request_key,
        )

    def execution(self, operation_id, execution_id):
        return self._call(
            "operation_execution_read", operation_id=operation_id, execution_id=execution_id
        )

    def cancel(self, operation_id, execution_id):
        return self._call(
            "operation_execution_cancel", operation_id=operation_id, execution_id=execution_id
        )


def python_reference():
    """Small discoverable guide; full authoring model schemas are a separate resource."""
    import inspect

    return {
        "globals": {
            "client": "chipload.client.Client scoped to this run's shop and caller grants",
            "inputs": "Submitted JSON object",
            "result": "Assign JSON-compatible output",
        },
        "client_methods": {
            name: str(inspect.signature(getattr(Client, name)))
            for name in ("shop", "identity", "reference", "call")
        },
        "shop_methods": {
            name: str(inspect.signature(method))
            for name, method in vars(ShopClient).items()
            if callable(method)
            and not name.startswith("_")
            and name not in {"run", "run_result", "cancel_run"}
        },
        "authoring_schema": "chipload://sdk/reference",
        "errors": "chipload.client.ServiceError has status, detail, code and retry_after. A failed/cancelled/interrupted run does not roll back completed calls. Inspect its call journal and service records before making more changes.",
        "limits": {
            "seconds": 30,
            "sdk_calls": 64,
            "request_bytes": 262144,
            "result_bytes": 262144,
            "log_bytes": 65536,
        },
        "execution": "Ordinary Python with installed Chipload SDK and supplied helper modules. No network, host mounts, credentials, runtime package installation or recursive code runs. Each call uses current explicit grants intersected with admission grants for the selected shop. Public discovery of other shops remains available.",
        "example": "shop = client.shop(inputs['shop_id'])\nresult = {'catalog': shop.catalog(), 'variables': shop.variables(inputs.get('query', {}))}",
        "workflow": "Read source, construct or edit a SourceProject with ordinary Python, save with expected_head, request a build, inspect its report, publish with expected_generation. Long builds can be inspected in a subsequent run. Use stable request keys for build and operation admission. Never infer physical qualification from a passing build.",
    }
