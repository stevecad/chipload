"""Bounded source projects and build contracts, independent of source execution."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import Field, StrictStr, model_validator

from .artifact import Artifact, Digest, canonical_json, digest
from .evaluate import EvaluationRequest, Outcome, evaluate
from .model import Id, Model, Observation, Timestamp
from .validation import Diagnostic

MAX_PROJECT_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024


class SourceFile(Model):
    path: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    content: Annotated[StrictStr, Field(max_length=128 * 1024)]

    @model_validator(mode="after")
    def safe_path(self) -> Self:
        parts = self.path.split("/")
        if (
            len(parts) > 8
            or any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", p) for p in parts)
            or PurePosixPath(self.path).suffix not in {".py", ".json", ".toml", ".txt", ".md"}
            or parts[0] in {"chipload", "chipload.py"}
        ):
            raise ValueError(
                "use a relative project path with safe names and a supported text suffix"
            )
        if "\0" in self.content:
            raise ValueError("source files cannot contain NUL")
        return self


class AcceptanceCase(Model):
    id: Id
    request: EvaluationRequest
    at: Timestamp
    observations: Annotated[tuple[Observation, ...], Field(max_length=1000)] = ()
    expected: Outcome
    reason_codes: Annotated[tuple[Id, ...], Field(max_length=100)] = ()


class SourceProject(Model):
    format: Literal["chipload-project-0.2-draft"] = "chipload-project-0.2-draft"
    shop_id: Id
    entrypoint: Annotated[
        StrictStr,
        Field(pattern=r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$", max_length=200),
    ]
    files: Annotated[tuple[SourceFile, ...], Field(min_length=1, max_length=128)]
    tests: Annotated[tuple[AcceptanceCase, ...], Field(max_length=100)] = ()

    @model_validator(mode="after")
    def bounded_unique_project(self) -> Self:
        paths = {f.path for f in self.files}
        if len(paths) != len(self.files):
            raise ValueError("source paths must be unique")
        # Files cannot also be ancestor directories, even on a case-insensitive client.
        folded = {p.casefold() for p in paths}
        if len(folded) != len(paths):
            raise ValueError("source paths must not differ only in letter case")
        for path in folded:
            if any(str(parent) in folded for parent in PurePosixPath(path).parents):
                raise ValueError("a source file cannot also be a directory")
        module = self.entrypoint.split(":")[0].replace(".", "/")
        if f"{module}.py" not in paths and f"{module}/__init__.py" not in paths:
            raise ValueError("entrypoint module must be supplied in the project")
        if len({case.id for case in self.tests}) != len(self.tests):
            raise ValueError("test identifiers must be unique")
        if len(canonical_json(self)) > MAX_PROJECT_BYTES:
            raise ValueError("project exceeds the serialized size limit")
        return self

    def to_json(self) -> bytes:
        return canonical_json(
            self.model_copy(update={"files": tuple(sorted(self.files, key=lambda f: f.path))})
        )

    @property
    def revision(self) -> str:
        return digest(self.to_json())


class CaseResult(Model):
    id: Id
    passed: bool
    actual: Outcome | None = None
    reason_codes: tuple[Id, ...] = ()
    error: str | None = None


class BuildReport(Model):
    source_revision: Digest
    worker_image: Annotated[StrictStr, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    status: Literal["succeeded", "failed"]
    artifact: Artifact | None = None
    diagnostics: tuple[Diagnostic, ...] = ()
    tests: tuple[CaseResult, ...] = ()

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.status == "succeeded" and (
            self.artifact is None or self.diagnostics or any(not t.passed for t in self.tests)
        ):
            raise ValueError("successful builds require a valid artifact and passing tests")
        if self.artifact and self.artifact.identity.source_sha256 != self.source_revision:
            raise ValueError("artifact source does not match the build")
        return self


def run_cases(artifact: Artifact, cases: tuple[AcceptanceCase, ...]) -> tuple[CaseResult, ...]:
    """Data-only assertions; source cannot replace the evaluator in this process."""
    results = []
    for case in cases:
        try:
            result = evaluate(artifact, case.request, at=case.at, observations=case.observations)
            codes = tuple(sorted({r.code for r in result.reasons}))
            results.append(
                CaseResult(
                    id=case.id,
                    passed=result.outcome == case.expected and set(case.reason_codes) <= set(codes),
                    actual=result.outcome,
                    reason_codes=codes,
                )
            )
        except (KeyError, ValueError) as error:
            results.append(CaseResult(id=case.id, passed=False, error=str(error)[:1000]))
    return tuple(results)


def unique_json(data: bytes) -> object:
    """Reject duplicate fields at worker/project trust boundaries."""
    import json

    def pairs(items: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    return json.loads(data, object_pairs_hook=pairs)
