"""Keycloak access-token verification and explicit, operator-owned shop grants."""

from __future__ import annotations

import ipaddress
import ssl
import threading
import time
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

import httpx
import jwt
from pydantic import Field, StrictInt, StrictStr, field_validator, model_validator

from .access import ACTIONS, Grant, Principal
from .cache import DEFAULT_CACHE_BYTES
from .model import Model
from .project import unique_json


class AuthenticationError(ValueError):
    pass


class IdentityUnavailable(RuntimeError):
    pass


class ServerSettings(Model):
    mode: Literal["development", "production"] = "production"
    database: Path = Path(".local/chipload.db")
    data_cache_bytes: Annotated[StrictInt, Field(ge=0, le=512 * 1024 * 1024)] = DEFAULT_CACHE_BYTES
    resource_url: StrictStr
    issuer: StrictStr
    jwks_url: StrictStr
    jwks_ca_file: Path | None = None
    trusted_proxy_ips: Annotated[tuple[StrictStr, ...], Field(max_length=16)] = ()
    role_client: StrictStr = "chipload"
    bindings: Annotated[tuple[Principal, ...], Field(max_length=1000)] = ()
    allowed_origins: tuple[StrictStr, ...] = ()

    @field_validator("trusted_proxy_ips")
    @classmethod
    def exact_proxy_addresses(cls, values):
        result = []
        for value in values:
            address = ipaddress.ip_address(value)
            address = getattr(address, "ipv4_mapped", None) or address
            if "%" in value or address.is_unspecified or address.is_multicast:
                raise ValueError("trusted proxies must be individual unicast IP addresses")
            result.append(str(address))
        if len(set(result)) != len(result):
            raise ValueError("trusted proxy addresses must be unique")
        return tuple(result)

    @field_validator("jwks_ca_file")
    @classmethod
    def absolute_ca_path(cls, value):
        if value is not None and not value.is_absolute():
            raise ValueError("JWKS CA bundle path must be absolute")
        return value

    @model_validator(mode="after")
    def endpoints_and_bindings(self) -> Self:
        for url in (self.resource_url, self.issuer, self.jwks_url, *self.allowed_origins):
            parsed = urlsplit(url)
            if (
                not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.scheme not in {"http", "https"}
                or (self.mode == "production" and parsed.scheme != "https")
            ):
                raise ValueError(
                    "configure absolute HTTPS endpoints without credentials or queries"
                )
            if (
                self.mode == "development"
                and parsed.scheme == "http"
                and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            ):
                raise ValueError("development HTTP endpoints must use loopback")
        if urlsplit(self.resource_url).path != "/mcp" or self.issuer.endswith("/"):
            raise ValueError("resource URL must end in /mcp; issuer must not end in /")
        if len({p.subject for p in self.bindings}) != len(self.bindings):
            raise ValueError("identity bindings must have unique subjects")
        return self

    @property
    def base_url(self) -> str:
        return self.resource_url[:-4]

    @property
    def metadata_url(self) -> str:
        return self.base_url + "/.well-known/oauth-protected-resource/mcp"

    def protected_resource_metadata(self) -> dict:
        return {
            "resource": self.resource_url,
            "resource_name": "Chipload",
            "authorization_servers": [self.issuer],
            "bearer_methods_supported": ["header"],
        }


class TokenVerifier:
    def __init__(self, settings: ServerSettings, *, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(
            timeout=3,
            follow_redirects=False,
            trust_env=False,
            verify=ssl.create_default_context(cafile=settings.jwks_ca_file)
            if settings.jwks_ca_file
            else True,
        )
        self._owned_client = client is None
        self._keys: dict[str, jwt.PyJWK] = {}
        self._valid_until = 0.0
        self._last_attempt = float("-inf")
        self._lock = threading.Lock()
        self._bindings = {p.subject: p for p in settings.bindings}

    def close(self):
        if self._owned_client:
            self.client.close()

    def _key(self, kid: str):
        with self._lock:
            current = time.monotonic()
            if current < self._valid_until and kid in self._keys:
                return self._keys[kid].key
            if current - self._last_attempt >= 5:
                self._last_attempt = current
                try:
                    with self.client.stream("GET", self.settings.jwks_url) as response:
                        response.raise_for_status()
                        data = bytearray()
                        for chunk in response.iter_bytes():
                            data.extend(chunk)
                            if len(data) > 256 * 1024:
                                raise ValueError("key set too large")
                    document = unique_json(bytes(data))
                    records = document["keys"]
                    if not isinstance(records, list) or not 1 <= len(records) <= 32:
                        raise ValueError("invalid key set")
                    keys = {}
                    for record in records:
                        if not isinstance(record, dict):
                            raise ValueError("invalid key record")
                        if (
                            record.get("kty") != "RSA"
                            or record.get("use", "sig") != "sig"
                            or record.get("alg", "RS256") != "RS256"
                        ):
                            continue
                        key_id = record.get("kid")
                        if (
                            not isinstance(key_id, str)
                            or not 1 <= len(key_id) <= 128
                            or key_id in keys
                        ):
                            raise ValueError("invalid key identifier")
                        key = jwt.PyJWK.from_dict(record, algorithm="RS256")
                        if key.key.key_size < 2048:
                            raise ValueError("RSA key too small")
                        keys[key_id] = key
                    if not keys:
                        raise ValueError("no usable signing keys")
                    self._keys = keys
                    self._valid_until = current + 300
                except (httpx.HTTPError, ValueError, KeyError, TypeError, jwt.PyJWTError) as error:
                    raise IdentityUnavailable("identity verification is unavailable") from error
            if current >= self._valid_until:
                raise IdentityUnavailable("identity verification is unavailable")
            if kid not in self._keys:
                raise AuthenticationError("invalid access token")
            return self._keys[kid].key

    def verify(self, token: str) -> Principal:
        if len(token) > 16 * 1024:
            raise AuthenticationError("invalid access token")
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if (
                header.get("alg") != "RS256"
                or not isinstance(kid, str)
                or not 1 <= len(kid) <= 128
                or header.get("crit")
            ):
                raise AuthenticationError("invalid access token")
            key = self._key(kid)
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.settings.resource_url,
                issuer=self.settings.issuer,
                options={"require": ["exp", "iat", "iss", "sub", "aud"]},
            )
            if (
                claims.get("typ") != "Bearer"
                or not isinstance(claims["sub"], str)
                or not 1 <= len(claims["sub"]) <= 200
                or any(type(claims.get(k)) is not int for k in ("exp", "iat"))
                or ("nbf" in claims and type(claims["nbf"]) is not int)
                or not 0 < claims["exp"] - claims["iat"] <= 900
            ):
                raise AuthenticationError("invalid access token")
        except (jwt.PyJWTError, ValueError, TypeError, KeyError) as error:
            raise AuthenticationError("invalid access token") from error
        roles = claims.get("resource_access", {})
        roles = roles.get(self.settings.role_client, {}) if isinstance(roles, dict) else {}
        roles = roles.get("roles", []) if isinstance(roles, dict) else []
        roles = (
            {role for role in roles if isinstance(role, str)} if isinstance(roles, list) else set()
        )
        ceiling = set(ACTIONS) if "shop:admin" in roles else set()
        if "shop:operate" in roles:
            ceiling.update({"operation:read", "operation:execute", "operation:cancel"})
        if "shop:read" in roles:
            ceiling.update(action for action in ACTIONS if action.endswith(":read"))
        binding = self._bindings.get(claims["sub"])
        return Principal(
            subject=claims["sub"],
            grants=tuple(
                Grant(
                    shop_id=grant.shop_id,
                    actions=tuple(a for a in grant.actions if a in ceiling),
                    operations=grant.operations,
                )
                for grant in (binding.grants if binding else ())
            ),
        )
