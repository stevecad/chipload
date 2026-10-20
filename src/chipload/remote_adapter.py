"""A bounded HTTPS receiver adapter with a scoped OpenBao AppRole credential."""

import argparse
import http.client
import os
import re
import resource
import selectors
import signal
import ssl
import subprocess
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, StrictInt, StrictStr, field_validator, model_validator

from .artifact import Digest, canonical_json, digest
from .execution_model import MAX_EXECUTION_BYTES, AdapterResult
from .model import Fact, Id, Model, Timestamp
from .project import unique_json

MAX_WIRE_BYTES = 96 * 1024
FileName = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")]
SecretPath = Annotated[
    StrictStr, Field(max_length=512, pattern=r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*$")
]


class Contract(Model):
    shop_id: Id
    operation_id: Id
    command: Id
    effect: Literal["read", "write"]


class OpenBao(Model):
    origin: Annotated[StrictStr, Field(max_length=2048)]
    ca_file: FileName | None = None
    auth_mount: SecretPath = "approle"
    kv_mount: SecretPath
    secret_path: SecretPath
    field: Id = "token"
    policy: Id

    @field_validator("origin")
    @classmethod
    def secure_origin(cls, value):
        return checked_origin(value)


class HttpAdapterConfig(Model):
    protocol: Literal["chipload-receiver-1"] = "chipload-receiver-1"
    id: Id
    receiver_id: Id
    origin: Annotated[StrictStr, Field(max_length=2048)]
    ca_file: FileName | None = None
    contracts: tuple[Contract, ...] = Field(min_length=1, max_length=64)
    secrets: OpenBao
    timeout_seconds: Annotated[StrictInt, Field(ge=1, le=30)] = 10

    @field_validator("origin")
    @classmethod
    def secure_origin(cls, value):
        return checked_origin(value)

    @model_validator(mode="after")
    def unique_contracts(self):
        if len({(c.shop_id, c.operation_id) for c in self.contracts}) != len(self.contracts):
            raise ValueError("adapter contracts must name unique shop/operation pairs")
        return self

    @property
    def fingerprint(self):
        return digest(canonical_json(self))


def checked_origin(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.port == 0
        or not re.fullmatch(r"https://[A-Za-z0-9.\[\]:-]+", value)
    ):
        raise ValueError("adapter endpoints must be HTTPS origins without paths or credentials")
    return value


class ReceiverRequest(Model):
    protocol: Literal["chipload-receiver-1"] = "chipload-receiver-1"
    receiver_id: Id
    execution_id: Digest
    intent_sha256: Digest
    shop_id: Id
    operation_id: Id
    actor: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    command: Id
    effect: Literal["read", "write"]
    mode: Literal["live"] = "live"
    deadline: Timestamp
    inputs: tuple[Fact, ...] = Field(max_length=100)


class ReceiverResponse(Model):
    protocol: Literal["chipload-receiver-1"]
    receiver_id: Id
    execution_id: Digest
    intent_sha256: Digest
    mode: Literal["live"]
    result: AdapterResult


def private_bytes(path, maximum):
    # Credentials are replaced atomically in a mounted private directory.
    import stat

    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("adapter files must be private regular files owned by the executor")
        value = stream.read(maximum + 1)
        if len(value) > maximum:
            raise ValueError("adapter file exceeds its byte limit")
        return value


def certificate(credentials, name):
    context = ssl.create_default_context()
    if name:
        context = ssl.create_default_context(
            cadata=private_bytes(credentials / name, 256 * 1024).decode("ascii")
        )
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def exchange(origin, context, method, path, *, body=None, headers=None, maximum=MAX_WIRE_BYTES):
    """One fixed-origin request. No redirects, proxies, decompression, or retries."""
    parsed = urlsplit(origin)
    connection = http.client.HTTPSConnection(
        parsed.hostname, parsed.port or 443, timeout=2, context=context
    )
    try:
        connection.request(
            method,
            path,
            body=canonical_json(body) if body is not None else None,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Content-Type": "application/json",
                **(headers or {}),
            },
        )
        response = connection.getresponse()
        if response.getheader("Content-Encoding", "identity") != "identity":
            raise ValueError("encoded adapter responses are not supported")
        data = response.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("adapter response exceeds its byte limit")
        return response.status, unique_json(data) if data else None
    finally:
        connection.close()


def token_value(value):
    if (
        not isinstance(value, str)
        or len(value) > 4096
        or not re.fullmatch(r"[A-Za-z0-9._~+/-]{1,4096}=*", value)
    ):
        raise ValueError("invalid workload credential")
    return value


def receiver_token(config: OpenBao, credentials: Path):
    context = certificate(credentials, config.ca_file)
    status, value = exchange(
        config.origin,
        context,
        "POST",
        f"/v1/auth/{config.auth_mount}/login",
        body={
            "role_id": token_value(
                private_bytes(credentials / "role-id", 4096).decode("ascii").strip()
            ),
            "secret_id": token_value(
                private_bytes(credentials / "secret-id", 4096).decode("ascii").strip()
            ),
        },
    )
    if status != 200 or not isinstance(value, dict) or not isinstance(value.get("auth"), dict):
        raise ValueError("workload authentication unavailable")
    auth = value["auth"]
    token = token_value(auth.get("client_token"))
    try:
        if (
            auth.get("policies") != [config.policy]
            or auth.get("identity_policies")
            or type(auth.get("lease_duration")) is not int
            or not 0 < auth["lease_duration"] <= 300
        ):
            raise ValueError("workload token exceeds its configured scope or lifetime")
        status, value = exchange(
            config.origin,
            context,
            "GET",
            f"/v1/{config.kv_mount}/data/{config.secret_path}",
            headers={"X-Vault-Token": token},
        )
        if status != 200:
            raise ValueError("adapter credential unavailable")
        metadata = value["data"]["metadata"]
        if (
            metadata.get("destroyed") is not False
            or metadata.get("deletion_time")
            or type(metadata.get("version")) is not int
            or metadata["version"] < 1
        ):
            raise ValueError("adapter credential version is not active")
        return token_value(value["data"]["data"][config.field])
    finally:
        # No token is cached or persisted. An interrupted revoke is bounded by its TTL.
        try:
            exchange(
                config.origin,
                context,
                "POST",
                "/v1/auth/token/revoke-self",
                headers={"X-Vault-Token": token},
            )
        except Exception:
            pass


def perform(config, credentials, request, *, reconciliation, cancel_requested):
    try:
        token = receiver_token(config.secrets, credentials)
        context = certificate(credentials, config.ca_file)
    except Exception:
        return AdapterResult(
            state="indeterminate" if reconciliation else "rejected",
            code="adapter-credential-unavailable",
        )
    if not reconciliation and request.deadline <= datetime.now(timezone.utc):
        return AdapterResult(state="rejected", code="adapter-deadline-before-send")
    path = f"/v1/executions/{request.execution_id}"
    method, body = ("GET", None) if reconciliation else ("PUT", request.model_dump(mode="json"))
    if reconciliation and cancel_requested:
        path += "/cancel"
        method, body = (
            "POST",
            {"intent_sha256": request.intent_sha256, "receiver_id": request.receiver_id},
        )
    try:
        status, value = exchange(
            config.origin,
            context,
            method,
            path,
            body=body,
            headers={"Authorization": "Bearer " + token},
        )
        # Even an authenticated 404 cannot prove that an earlier dispatch had no effect.
        if status not in {200, 201, 202}:
            raise ValueError("receiver outcome is unavailable")
        response = ReceiverResponse.model_validate(value)
        if (
            response.execution_id != request.execution_id
            or response.intent_sha256 != request.intent_sha256
            or response.receiver_id != request.receiver_id
        ):
            raise ValueError("receiver returned another intent")
        result = response.result
        # A receiver must not accidentally return its bearer credential in output.
        if (
            token.encode() in canonical_json(result)
            or len(canonical_json(result)) > MAX_EXECUTION_BYTES
        ):
            raise ValueError("receiver output is not admissible")
        return result
    except Exception:
        return AdapterResult(state="indeterminate", code="receiver-outcome-unknown")


def bounded_worker(payload, seconds, stopping):
    """Bound wall time and pipe bytes, including DNS and slow response bodies."""
    encoded = canonical_json(payload)
    if len(encoded) > 128 * 1024:
        raise ValueError("adapter invocation exceeds its byte limit")
    deadline = time.monotonic() + seconds
    child = subprocess.Popen(
        [sys.executable, "-I", "-B", "-m", "chipload.remote_adapter"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd="/",
        env={"PATH": os.defpath, "LANG": "C.UTF-8"},
    )
    output, position = bytearray(), 0
    try:
        with selectors.DefaultSelector() as selector:
            for stream, events in (
                (child.stdin, selectors.EVENT_WRITE),
                (child.stdout, selectors.EVENT_READ),
            ):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, events)
            while selector.get_map():
                if time.monotonic() >= deadline or stopping.is_set():
                    raise TimeoutError("adapter call interrupted")
                for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                    if key.fileobj is child.stdin:
                        position += os.write(
                            child.stdin.fileno(), encoded[position : position + 16384]
                        )
                        if position == len(encoded):
                            selector.unregister(child.stdin)
                            child.stdin.close()
                    else:
                        chunk = os.read(child.stdout.fileno(), 16384)
                        output.extend(chunk)
                        if len(output) > MAX_EXECUTION_BYTES:
                            raise ValueError("adapter result exceeds its pipe limit")
                        if not chunk:
                            selector.unregister(child.stdout)
            child.wait(timeout=max(0.001, deadline - time.monotonic()))
            if child.returncode != 0:
                raise ValueError("adapter child failed")
        return AdapterResult.model_validate(unique_json(bytes(output)))
    finally:
        # Kill the process group even if its original leader exited unexpectedly.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=3)
        child.stdin.close()
        child.stdout.close()


class HttpAdapter:
    mode = "live"

    def __init__(self, config: HttpAdapterConfig, credentials: Path, stopping):
        self.config, self.credentials, self.stopping = config, credentials, stopping
        self.fingerprint = config.fingerprint
        self._retries = OrderedDict()

    def invoke(self, context, *, reconciliation):
        contract = Contract(
            shop_id=context.request.shop_id,
            operation_id=context.request.operation_id,
            command=context.operation.command,
            effect=context.operation.effect,
        )
        if contract not in self.config.contracts:
            return AdapterResult(
                state="indeterminate" if reconciliation else "rejected",
                code="adapter-contract-not-allowed",
            )
        request = ReceiverRequest(
            receiver_id=self.config.receiver_id,
            execution_id=context.execution_id,
            intent_sha256=context.intent_sha256,
            actor=context.actor,
            deadline=context.deadline,
            inputs=context.request.inputs,
            **contract.model_dump(mode="python"),
        )
        seconds = self.config.timeout_seconds
        if not reconciliation:
            seconds = min(seconds, (context.deadline - datetime.now(timezone.utc)).total_seconds())
        if seconds <= 0 or self.stopping.is_set():
            return AdapterResult(state="indeterminate", code="adapter-call-interrupted")
        try:
            return bounded_worker(
                {
                    "config": self.config.model_dump(mode="json"),
                    "credentials": str(self.credentials),
                    "request": request.model_dump(mode="json"),
                    "reconciliation": reconciliation,
                    "cancel_requested": context.cancel_requested,
                },
                seconds,
                self.stopping,
            )
        except Exception:
            return AdapterResult(state="indeterminate", code="adapter-call-interrupted")

    def execute(self, context):
        return self.invoke(context, reconciliation=False)

    def reconcile(self, context):
        previous = self._retries.get(context.execution_id)
        if previous and previous[0] == context.cancel_requested and time.monotonic() < previous[1]:
            return None
        result = self.invoke(context, reconciliation=True)
        if result.state != "indeterminate":
            self._retries.pop(context.execution_id, None)
            return result
        delay = (
            min(30, previous[2] * 2) if previous and previous[0] == context.cancel_requested else 2
        )
        self._retries[context.execution_id] = (
            context.cancel_requested,
            time.monotonic() + delay,
            delay,
        )
        self._retries.move_to_end(context.execution_id)
        while len(self._retries) > 1024:
            self._retries.popitem(last=False)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fingerprint", type=Path)
    args = parser.parse_args()
    if args.fingerprint:
        print(
            HttpAdapterConfig.model_validate(
                unique_json(private_bytes(args.fingerprint, 128 * 1024))
            ).fingerprint
        )
        return
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024**2, 256 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    data = sys.stdin.buffer.read(128 * 1024 + 1)
    if len(data) > 128 * 1024:
        raise ValueError("adapter invocation exceeds its byte limit")
    value = unique_json(data)
    result = perform(
        HttpAdapterConfig.model_validate(value["config"]),
        Path(value["credentials"]),
        ReceiverRequest.model_validate(value["request"]),
        reconciliation=value["reconciliation"],
        cancel_requested=value["cancel_requested"],
    )
    encoded = canonical_json(result)
    if len(encoded) > MAX_EXECUTION_BYTES:
        raise ValueError("adapter result exceeds its byte limit")
    sys.stdout.buffer.write(encoded)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("adapter request failed") from None
