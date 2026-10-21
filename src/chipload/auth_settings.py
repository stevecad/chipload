"""Identity configuration shared with clients without server dependencies."""

import ipaddress
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, StrictInt, StrictStr, field_validator, model_validator

from .access import Principal
from .cache import DEFAULT_CACHE_BYTES
from .model import Model
from .peer_model import InstanceId


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
    instance_id: InstanceId | None = Field(default=None, exclude_if=lambda value: value is None)

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
