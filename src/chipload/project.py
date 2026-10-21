"""Bounded source projects and build contracts, independent of source execution."""

from __future__ import annotations

import base64
import binascii
import re
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import Field, StrictStr, model_validator

from .artifact import Artifact, Digest, canonical_json, digest
from .complexity import target_budgets
from .evaluate import EvaluationRequest, Outcome, evaluate
from .limits import MAX_CASE_WORK, MAX_DIAGNOSTICS, bounded_json
from .model import Id, Model, Observation, Timestamp
from .python_checks import PythonEvaluation, suite_plans
from .validation import Diagnostic

MAX_PROJECT_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_ASSET_BYTES = 1024 * 1024


def _safe_path(path: str) -> None:
    parts = path.split("/")
    if (
        len(parts) > 8
        or any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", p) for p in parts)
        or parts[0].casefold() in {"chipload", "chipload.py"}
    ):
        raise ValueError("use a relative project path with safe names outside the chipload package")


class SourceFile(Model):
    path: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    content: Annotated[StrictStr, Field(max_length=128 * 1024)]

    @model_validator(mode="after")
    def safe_path(self) -> Self:
        _safe_path(self.path)
        if PurePosixPath(self.path).suffix not in {".py", ".json", ".toml", ".txt", ".md"}:
            raise ValueError("use a supported text suffix")
        if "\0" in self.content:
            raise ValueError("source files cannot contain NUL")
        return self


class SourceAsset(Model):
    """Opaque private bytes, materialized only inside a source worker."""

    path: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    content_base64: Annotated[StrictStr, Field(max_length=4 * ((MAX_ASSET_BYTES + 2) // 3))]

    @model_validator(mode="after")
    def valid_asset(self) -> Self:
        _safe_path(self.path)
        self.read_bytes()
        return self

    def read_bytes(self) -> bytes:
        try:
            content = base64.b64decode(self.content_base64, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("asset content must be canonical standard base64") from None
        if len(content) > MAX_ASSET_BYTES:
            raise ValueError("asset exceeds the decoded size limit")
        if base64.b64encode(content).decode("ascii") != self.content_base64:
            raise ValueError("asset content must be canonical standard base64")
        return content

    @classmethod
    def from_bytes(cls, *, path: str, content: bytes) -> SourceAsset:
        if not isinstance(content, bytes) or len(content) > MAX_ASSET_BYTES:
            raise ValueError("asset content must be bytes within the decoded size limit")
        return cls(path=path, content_base64=base64.b64encode(content).decode("ascii"))


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
    # Omit an absent pin to preserve existing source bytes and revision IDs.
    dependencies_sha256: Digest | None = Field(default=None, exclude_if=lambda value: value is None)
    # Existing text-only projects retain their canonical bytes and revision IDs.
    assets: Annotated[tuple[SourceAsset, ...], Field(max_length=128)] = Field(
        default=(), exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def bounded_unique_project(self) -> Self:
        entries = (*self.files, *self.assets)
        if len(entries) > 128:
            raise ValueError("project exceeds the combined file and asset count limit")
        paths = {f.path for f in entries}
        if len(paths) != len(entries):
            raise ValueError("project paths must be unique")
        # Files cannot also be ancestor directories, even on a case-insensitive client.
        folded = {p.casefold() for p in paths}
        if len(folded) != len(paths):
            raise ValueError("source paths must not differ only in letter case")
        for path in folded:
            if any(str(parent) in folded for parent in PurePosixPath(path).parents):
                raise ValueError("a source file cannot also be a directory")
        module = self.entrypoint.split(":")[0].replace(".", "/")
        source_paths = {f.path for f in self.files}
        if f"{module}.py" not in source_paths and f"{module}/__init__.py" not in source_paths:
            raise ValueError("entrypoint module must be supplied in the project")
        if len({case.id for case in self.tests}) != len(self.tests):
            raise ValueError("test identifiers must be unique")
        if len(canonical_json(self)) > MAX_PROJECT_BYTES:
            raise ValueError("project exceeds the serialized size limit")
        return self

    def to_json(self) -> bytes:
        return canonical_json(
            self.model_copy(update={
                "files": tuple(sorted(self.files, key=lambda f: f.path)),
                "assets": tuple(sorted(self.assets, key=lambda f: f.path)),
            })
        )

    @property
    def revision(self) -> str:
        return digest(self.to_json())


class CaseResult(Model):
    id: Id
    passed: bool
    actual: Outcome | None = None
    reason_codes: tuple[Id, ...] = Field(default=(), max_length=100)
    error: Annotated[StrictStr, Field(max_length=1000)] | None = None
    python_evaluation: PythonEvaluation | None = Field(default=None, exclude_if=lambda value: value is None)


class BuildReport(Model):
    source_revision: Digest
    worker_image: Annotated[StrictStr, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    status: Literal["succeeded", "failed"]
    artifact: Artifact | None = None
    diagnostics: tuple[Diagnostic, ...] = Field(default=(), max_length=MAX_DIAGNOSTICS)
    tests: tuple[CaseResult, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.status == "succeeded" and (
            self.artifact is None or self.diagnostics or any(not t.passed for t in self.tests)
        ):
            raise ValueError("successful builds require a valid artifact and passing tests")
        if self.artifact and self.artifact.identity.source_sha256 != self.source_revision:
            raise ValueError("artifact source does not match the build")
        return self


def run_cases(artifact: Artifact, cases: tuple[AcceptanceCase, ...], *,
              python_evaluations: tuple[PythonEvaluation, ...] = ()) -> tuple[CaseResult, ...]:
    """Data-only assertions; source cannot replace the evaluator in this process."""
    budgets = target_budgets(artifact.shop)
    suite_plans(artifact.shop, cases)
    if python_evaluations and len(python_evaluations) != len(cases):
        raise ValueError("Python evaluations must match the acceptance cases")
    work = sum(budgets.get((c.request.target_kind, c.request.target), (0, 0))[0] for c in cases)
    if len(cases) > 100 or work > MAX_CASE_WORK:
        # Do not silently skip tests or treat a budget failure as domain rejection.
        raise ValueError(f"Acceptance suite exceeds the {MAX_CASE_WORK} work / 100 case limit")
    results = []
    for index, case in enumerate(cases):
        python_evaluation = python_evaluations[index] if python_evaluations else None
        try:
            result = evaluate(artifact, case.request, at=case.at, observations=case.observations,
                              python_evaluation=python_evaluation)
            codes = tuple(sorted({r.code for r in result.reasons}))
            results.append(
                CaseResult(
                    id=case.id,
                    passed=result.outcome == case.expected and set(case.reason_codes) <= set(codes),
                    actual=result.outcome,
                    reason_codes=codes,
                    python_evaluation=python_evaluation,
                )
            )
        except (KeyError, ValueError) as error:
            results.append(CaseResult(id=case.id, passed=False, error=str(error)[:1000]))
    return tuple(results)


def unique_json(data: bytes) -> object:
    """Shared bounded decoder for network, project, and worker data."""
    return bounded_json(data)
