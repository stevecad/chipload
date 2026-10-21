"""Connector-only OpenBao credentials and verified Keycloak workload tokens."""

import math
import os
import stat
import threading
import time
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import httpx
import jwt
from pydantic import Field, StrictStr, TypeAdapter, model_validator

from .access import AuthenticationError, IdentityUnavailable
from .auth import ServerSettings, TokenVerifier
from .client import ServiceError, TransportError
from .federation_wire import EgressPolicy, WireJob, WireToken, request
from .model import Id, Model
from .remote_adapter import OpenBao, private_bytes, token_value


class CredentialUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Connector credential is unavailable")


class WorkloadIdentity(Model):
    client_id: Id
    subject: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    trust: ServerSettings
    secrets: OpenBao
    policy: EgressPolicy = EgressPolicy(seconds=5)

    @model_validator(mode="after")
    def identity_only(self):
        if self.trust.mode != "production" or self.trust.bindings or self.trust.instance_id is not None:
            raise ValueError("connector trust must contain production identity settings without shop grants")
        # Check both fixed paths now, before credentials can be read or sent.
        for endpoint in (self.trust.issuer + "/protocol/openid-connect/token", self.trust.jwks_url):
            parsed = urlsplit(endpoint)
            WireJob(origin="https://" + parsed.netloc, path=parsed.path, method="GET", policy=self.policy)
        return self


def budget(policy, deadline):
    seconds = min(policy.seconds, math.floor(deadline - time.monotonic() - 2))
    if seconds < 1:
        raise CredentialUnavailable()
    return policy.model_copy(update={"seconds": seconds})


def secret_value(config, credentials, policy, deadline):
    """Read one configured KV-v2 field; never grant operator or equipment authority."""
    credentials = Path(credentials)
    if config.ca_file:
        ca_file = credentials / config.ca_file
        private_bytes(ca_file, 256 * 1024)
        policy = policy.model_copy(update={"ca_file": ca_file})
    role = token_value(private_bytes(credentials / "role-id", 4096).decode("ascii").strip())
    secret = token_value(private_bytes(credentials / "secret-id", 4096).decode("ascii").strip())
    value = request(config.origin, "POST", f"/v1/auth/{config.auth_mount}/login",
                    policy=budget(policy, deadline), body={"role_id": role, "secret_id": secret},
                    sensitive=(role, secret))
    auth = value["auth"]
    if not isinstance(auth, dict):
        raise CredentialUnavailable()
    token = token_value(auth["client_token"])
    try:
        if (auth.get("policies") != [config.policy] or auth.get("identity_policies")
                or type(auth.get("lease_duration")) is not int
                or not 0 < auth["lease_duration"] <= 300):
            raise CredentialUnavailable()
        value = request(config.origin, "GET", f"/v1/{config.kv_mount}/data/{config.secret_path}",
                        policy=budget(policy, deadline), token=token, credential_header="vault")
        metadata = value["data"]["metadata"]
        if (not isinstance(metadata, dict) or metadata.get("destroyed") is not False or metadata.get("deletion_time")
                or type(metadata.get("version")) is not int or metadata["version"] < 1):
            raise CredentialUnavailable()
        return token_value(value["data"]["data"][config.field])
    finally:
        try:
            request(config.origin, "POST", "/v1/auth/token/revoke-self", policy=budget(policy, deadline),
                    token=token, credential_header="vault")
        except Exception:
            # A lost revoke is contained by the verified five-minute token lifetime.
            pass


class KeyTransport(httpx.BaseTransport):
    """Let the shared verifier use bounded, pinned HTTPS for its fixed JWKS URL."""

    def __init__(self, url, policy):
        self.url, self.policy = url, policy
        self.deadline = 0

    def handle_request(self, incoming):
        if incoming.method != "GET" or str(incoming.url) != self.url:
            raise httpx.UnsupportedProtocol("Unexpected identity request")
        parsed = urlsplit(self.url)
        try:
            value = request("https://" + parsed.netloc, "GET", parsed.path,
                            policy=budget(self.policy, self.deadline))
        except (ValueError, ServiceError, TransportError, CredentialUnavailable):
            raise httpx.ConnectError("Identity verification is unavailable") from None
        return httpx.Response(200, json=value, request=incoming)


class WorkloadToken:
    """Refresh one workload identity; credentials and bearer tokens stay in this process."""

    def __init__(self, config: WorkloadIdentity, credentials: Path):
        self.config, self.credentials = config, Path(credentials)
        if not self.credentials.is_absolute():
            raise ValueError("connector credentials require an absolute directory")
        info = self.credentials.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("connector credentials require an owner-only directory")
        self.transport = KeyTransport(config.trust.jwks_url, config.policy)
        self.http = httpx.Client(transport=self.transport)
        self.verifier = TokenVerifier(config.trust, client=self.http)
        self._token, self._until = None, 0
        self._closed = False
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            if self._closed:
                raise CredentialUnavailable()
            if self._token is not None and time.monotonic() < self._until:
                return self._token
            self._token, self._until = None, 0
            started = time.monotonic()
            deadline = started + 30
            try:
                secret = secret_value(self.config.secrets, self.credentials, self.config.policy, deadline)
                parsed = urlsplit(self.config.trust.issuer)
                value = request("https://" + parsed.netloc, "POST",
                    parsed.path + "/protocol/openid-connect/token", policy=budget(self.config.policy, deadline),
                    encoding="form", body={"grant_type": "client_credentials", "client_id": self.config.client_id,
                                           "client_secret": secret}, sensitive=(secret,))
                token = TypeAdapter(WireToken).validate_python(value["access_token"])
                self.transport.deadline = deadline
                principal = self.verifier.verify(token)
                claims = jwt.decode(token, options={"verify_signature": False})
                if (principal.subject != self.config.subject or claims.get("azp") != self.config.client_id
                        or not isinstance(value.get("token_type"), str) or value["token_type"].lower() != "bearer"
                        or type(value.get("expires_in")) is not int or not 0 < value["expires_in"] <= 900
                        or claims["exp"] - time.time() <= 15):
                    raise CredentialUnavailable()
                lifetime = min(started + value["expires_in"] - time.monotonic(), claims["exp"] - time.time())
                self._token, self._until = token, time.monotonic() + max(0, lifetime - 30)
                return token
            except (ValueError, KeyError, TypeError, OSError, ServiceError, TransportError,
                    AuthenticationError, IdentityUnavailable, jwt.PyJWTError, CredentialUnavailable):
                raise CredentialUnavailable() from None

    def invalidate(self):
        with self._lock:
            self._token, self._until = None, 0

    def close(self):
        with self._lock:
            self._closed = True
            self._token, self._until = None, 0
            self.http.close()
