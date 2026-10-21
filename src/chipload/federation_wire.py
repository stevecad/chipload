"""Bounded HTTPS in a disposable process, including DNS and TLS setup.

Only trusted connector code constructs jobs. Shop programs do not receive this
process's network namespace, credentials, policy or filesystem authority.
"""

import http.client
import ipaddress
import re
import resource
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, Field, StrictInt, StrictStr, field_validator

from .artifact import canonical_json
from .client import ServiceError, TransportError
from .limits import MAX_JSON_BYTES, bounded_json
from .model import Model

MAX_BODY_BYTES = MAX_JSON_BYTES - 4096
MAX_JOB_BYTES = 384 * 1024


def checked_origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or parsed.port == 0 or not re.fullmatch(r"https://[A-Za-z0-9.\[\]:-]+/?", value)):
        raise ValueError("configure an HTTPS origin without paths or credentials")
    host = parsed.hostname.lower()
    if ":" in host:
        host = "[" + ipaddress.ip_address(host).compressed + "]"
    return "https://" + host + (f":{parsed.port}" if parsed.port not in (None, 443) else "")


Origin = Annotated[StrictStr, Field(max_length=2048), AfterValidator(checked_origin)]


class EgressPolicy(Model):
    private_origins: tuple[Origin, ...] = Field(default=(), max_length=32)
    public_ports: tuple[Annotated[StrictInt, Field(ge=1, le=65535)], ...] = Field(default=(443,), max_length=16)
    ca_file: Path | None = None
    seconds: Annotated[StrictInt, Field(ge=1, le=20)] = 10


class WireJob(Model):
    origin: Origin
    policy: EgressPolicy
    method: Literal["GET", "POST", "PUT", "DELETE"]
    path: Annotated[StrictStr, Field(max_length=512, pattern=r"^/(?:[A-Za-z0-9_.-]+/?)*$")]
    body: dict | None = None
    token: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9._~+/-]{1,16384}=*$", max_length=16384)] | None = None

    @field_validator("path")
    @classmethod
    def canonical_path(cls, value):
        if value != "/" and any(part in {"", ".", ".."} for part in value[1:].split("/")):
            raise ValueError("request paths require canonical segments")
        return value


def resolved_addresses(origin, policy):
    """Validate every answer and return the exact socket addresses to connect."""
    parsed = urlsplit(origin)
    port = parsed.port or 443
    private = origin in policy.private_origins
    if not private and port not in policy.public_ports:
        raise ValueError("destination port is not permitted")
    answers = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    if not answers or len(answers) > 32:
        raise ValueError("unusable DNS answer")
    result = []
    for family, kind, protocol, _, address in answers:
        if family not in (socket.AF_INET, socket.AF_INET6) or kind != socket.SOCK_STREAM:
            raise ValueError("unsupported socket address")
        numeric = ipaddress.ip_address(address[0])
        normalized = getattr(numeric, "ipv4_mapped", None) or numeric
        if normalized.is_unspecified or normalized.is_multicast or normalized.is_link_local:
            raise ValueError("destination address is not permitted")
        if not private and not normalized.is_global:
            raise ValueError("destination address is not public")
        if family == socket.AF_INET6 and address[3]:
            raise ValueError("scoped destination address is not permitted")
        entry = (family, protocol, address)
        if entry not in result:
            result.append(entry)
    return result


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, *, addresses, context, timeout):
        super().__init__(host, port, context=context, timeout=timeout)
        self.addresses = addresses

    def connect(self):
        last = None
        for family, protocol, address in self.addresses:
            sock = socket.socket(family, socket.SOCK_STREAM, protocol)
            try:
                sock.settimeout(self.timeout)
                sock.connect(address)
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                return
            except OSError as error:
                sock.close()
                last = error
        raise last or OSError("no destination address")


def exchange(job: WireJob):
    """Child-only exchange: no proxies, redirects, compression or HTTP retries."""
    parsed = urlsplit(job.origin)
    addresses = resolved_addresses(job.origin, job.policy)
    context = ssl.create_default_context(cafile=job.policy.ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    connection = PinnedHTTPS(parsed.hostname, parsed.port or 443, addresses=addresses,
                             context=context, timeout=job.policy.seconds)
    headers = {"Accept": "application/json", "Accept-Encoding": "identity", "Content-Type": "application/json"}
    if job.token:
        headers["Authorization"] = "Bearer " + job.token
    encoded = canonical_json(job.body) if job.body is not None else None
    deadline = time.monotonic() + job.policy.seconds
    try:
        connection.request(job.method, job.path, body=encoded, headers=headers)
        response = connection.getresponse()
        if response.getheader("Content-Encoding", "identity") != "identity":
            raise ValueError("encoded response is not supported")
        if response.getheader("Content-Type", "").split(";", 1)[0].lower() != "application/json":
            raise ValueError("JSON response required")
        data = bytearray()
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("response deadline")
            chunk = response.read1(min(65536, MAX_BODY_BYTES + 1 - len(data)))
            data.extend(chunk)
            if len(data) > MAX_BODY_BYTES:
                raise ValueError("response byte limit")
            if not chunk:
                break
        if job.token and job.token.encode() in data:
            raise ValueError("credential echoed by destination")
        value = bounded_json(bytes(data), max_bytes=MAX_BODY_BYTES)
        if not isinstance(value, dict):
            raise ValueError("response envelope required")
        return {"status": response.status, "value": value}
    finally:
        connection.close()


def request(origin, method, path, *, policy=None, body=None, token=None):
    """One attempt, with a hard process deadline that also contains DNS stalls."""
    job = WireJob(origin=origin, policy=policy or EgressPolicy(), method=method, path=path,
                  body=body, token=token)
    encoded = canonical_json(job)
    if len(encoded) > MAX_JOB_BYTES:
        raise ValueError("network request byte limit")
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "chipload.federation_wire"], input=encoded,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=job.policy.seconds + 2, check=False,
        )
        if completed.returncode or len(completed.stdout) > MAX_JSON_BYTES:
            raise ValueError("unusable network reply")
        result = bounded_json(completed.stdout)
        if (not isinstance(result, dict) or set(result) != {"status", "value"}
                or type(result["status"]) is not int or not isinstance(result["value"], dict)):
            raise ValueError("invalid network reply")
    except Exception:
        raise TransportError("No usable federation response") from None
    if not 200 <= result["status"] < 300:
        raise ServiceError(result["status"], result["value"])
    return result["value"]


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (384 * 1024**2, 384 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    try:
        raw = sys.stdin.buffer.read(MAX_JOB_BYTES + 1)
        if len(raw) > MAX_JOB_BYTES:
            raise ValueError("request byte limit")
        job = WireJob.model_validate(bounded_json(raw, max_bytes=MAX_JOB_BYTES))
        sys.stdout.buffer.write(canonical_json(exchange(job)))
    except Exception:
        # Never log URLs, query bodies, credential paths or remote response text.
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
