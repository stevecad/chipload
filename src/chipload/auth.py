"""Keycloak access-token verification and explicit, operator-owned shop grants."""

from __future__ import annotations

import ssl
import threading
import time

import httpx
import jwt

from .access import ACTIONS, AuthenticationError, Grant, IdentityUnavailable, Principal
from .auth_settings import ServerSettings as ServerSettings
from .project import unique_json


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
            ceiling.update({"operation:read", "operation:execute", "operation:cancel", "evaluation:run", "code:run", "code:read", "code:cancel"})
        if "shop:connect" in roles:
            ceiling.add("network:dispatch")
        if "shop:operate" in roles:
            ceiling.add("network:query")
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
