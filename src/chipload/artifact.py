"""Compile validated models to content-addressed data, without executing source."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StrictStr

from .model import Model, Shop
from .validation import validate_shop

Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
SDK_VERSION = "0.2.0.dev0"


def sdk_fingerprint() -> str:
    """Identity of the installed SDK and broker source used for validation."""
    root = Path(__file__).resolve().parent
    return digest(
        b"".join(
            path.name.encode() + b"\0" + path.read_bytes() + b"\0"
            for path in sorted(root.glob("*.py"))
        )
    )


def canonical_json(value: Model | dict) -> bytes:
    if isinstance(value, Model):
        value = value.model_dump(mode="json")
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class BuildIdentity(Model):
    source_sha256: Digest
    dependencies_sha256: Digest
    sdk_sha256: Digest
    runtime: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    sdk_version: Literal["0.2.0.dev0"] = SDK_VERSION


class Artifact(Model):
    format: Literal["chipload-artifact-0.2-draft"] = "chipload-artifact-0.2-draft"
    identity: BuildIdentity
    shop: Shop
    model_sha256: Digest
    build_sha256: Digest

    def to_json(self) -> bytes:
        return canonical_json(self)


def compile_shop(shop: Shop, identity: BuildIdentity) -> Artifact:
    """Compile already constructed data. This function never imports shop source."""
    # Revalidate even callers who used model_construct or model_copy(update=...).
    shop = Shop.model_validate(shop.model_dump(mode="python"))
    identity = BuildIdentity.model_validate(identity.model_dump(mode="python"))
    validate_shop(shop)
    model_hash = digest(canonical_json(shop))
    build_hash = digest(
        canonical_json({"model_sha256": model_hash, "identity": identity.model_dump(mode="json")})
    )
    return Artifact(identity=identity, shop=shop, model_sha256=model_hash, build_sha256=build_hash)


def read_artifact(data: bytes, *, max_bytes: int = 8 * 1024 * 1024) -> Artifact:
    """Validate a stored artifact's shape, graph, and digests before serving it."""
    if len(data) > max_bytes:
        raise ValueError("artifact exceeds the size limit")

    def unique_keys(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    payload = json.loads(data, object_pairs_hook=unique_keys)
    artifact = Artifact.model_validate(payload)
    expected = compile_shop(artifact.shop, artifact.identity)
    if (
        artifact.model_sha256 != expected.model_sha256
        or artifact.build_sha256 != expected.build_sha256
    ):
        raise ValueError("artifact digest mismatch")
    return artifact
