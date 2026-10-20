"""Docker build broker. Only this trusted component needs Docker access."""

from __future__ import annotations

import os
import re
import selectors
import subprocess
import time
import uuid
from dataclasses import dataclass

from pydantic import ValidationError

from .artifact import BuildIdentity, compile_shop, sdk_fingerprint
from .model import Shop
from .project import (
    MAX_OUTPUT_BYTES,
    BuildReport,
    SourceProject,
    run_cases,
    unique_json,
)
from .validation import Diagnostic, ShopValidationError


@dataclass(frozen=True)
class WorkerEnvironment:
    image: str
    sdk_sha256: str
    dependencies_sha256: str
    python_version: str

    @classmethod
    def inspect(cls, image: str) -> WorkerEnvironment:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise ValueError("worker image must be a local immutable sha256 image ID")
        result = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{json .Config.Labels}}"],
            capture_output=True,
            check=True,
            timeout=15,
        )
        labels = unique_json(result.stdout)
        if not isinstance(labels, dict) or labels.get("org.chipload.worker-protocol") != "1":
            raise ValueError("image is not a configured Chipload worker")
        sdk = labels.get("org.chipload.sdk-sha256", "")
        dependencies = labels.get("org.chipload.dependencies-sha256", "")
        python = labels.get("org.chipload.python-version", "")
        if not all(re.fullmatch(r"[0-9a-f]{64}", value) for value in (sdk, dependencies)):
            raise ValueError("worker fingerprints are missing")
        if not re.fullmatch(r"3\.\d+\.\d+", python):
            raise ValueError("worker Python version is missing")
        return cls(image, sdk, dependencies, python)


class SandboxError(RuntimeError):
    pass


def exchange(command: list[str], payload: bytes, *, seconds: float) -> tuple[int, bytes, bytes]:
    """Bound stdin, output memory, and wall time without blocking on full pipes."""
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    output, errors = bytearray(), bytearray()
    sent = 0
    deadline = time.monotonic() + seconds
    try:
        with selectors.DefaultSelector() as selector:
            for stream, events in (
                (process.stdin, selectors.EVENT_WRITE),
                (process.stdout, selectors.EVENT_READ),
                (process.stderr, selectors.EVENT_READ),
            ):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, events)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SandboxError("worker exceeded its wall-time limit")
                for key, events in selector.select(min(remaining, 0.25)):
                    stream = key.fileobj
                    if events & selectors.EVENT_WRITE:
                        try:
                            sent += os.write(stream.fileno(), payload[sent : sent + 65536])
                        except BrokenPipeError:
                            sent = len(payload)
                        if sent == len(payload):
                            selector.unregister(stream)
                            stream.close()
                    else:
                        chunk = os.read(stream.fileno(), 65536)
                        if not chunk:
                            selector.unregister(stream)
                            stream.close()
                            continue
                        target = output if stream is process.stdout else errors
                        target.extend(chunk)
                        limit = MAX_OUTPUT_BYTES if target is output else 64 * 1024
                        if len(target) > limit:
                            raise SandboxError("worker exceeded its output limit")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SandboxError("worker exceeded its wall-time limit")
            return process.wait(timeout=remaining), bytes(output), bytes(errors)
    except subprocess.TimeoutExpired as error:
        raise SandboxError("worker exceeded its wall-time limit") from error
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if not stream.closed:
                stream.close()


class DockerBuilder:
    def __init__(self, environment: WorkerEnvironment, *, seconds: float = 30):
        if not 0 < seconds <= 120:
            raise ValueError("worker wall-time limit must be between 0 and 120 seconds")
        if environment.sdk_sha256 != sdk_fingerprint():
            raise ValueError("worker and broker SDKs differ; rebuild the worker image")
        self.environment = environment
        self.seconds = seconds

    def create_command(self, name: str) -> list[str]:
        return [
            "docker",
            "create",
            "--name",
            name,
            "--label",
            "org.chipload.worker=true",
            "--network",
            "none",
            "--user",
            "65532:65532",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--pids-limit",
            "64",
            "--memory",
            "256m",
            "--memory-swap",
            "256m",
            "--cpus",
            "1",
            "--ulimit",
            "nofile=128:128",
            "--ulimit",
            "core=0:0",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777",
            "--log-driver",
            "none",
            "--stop-timeout",
            "1",
            "--workdir",
            "/tmp",
            "--entrypoint",
            "python",
            "-i",
            self.environment.image,
            "-I",
            "-B",
            "-m",
            "chipload.worker",
        ]

    def remove(self, job_id: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ValueError("invalid worker job ID")
        name = f"chipload-build-{job_id}"
        cleanup = subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=15)
        if cleanup.returncode != 0 and b"No such container" not in cleanup.stderr:
            raise SandboxError(f"could not remove worker container {name}")

    def build(self, project: SourceProject, *, job_id: str | None = None) -> BuildReport:
        # Validate unchecked model copies before passing data across a boundary.
        project = SourceProject.model_validate(project.model_dump(mode="python"))
        job_id = job_id or uuid.uuid4().hex
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ValueError("invalid worker job ID")
        name = f"chipload-build-{job_id}"
        diagnostics = []
        artifact = None
        tests = ()
        try:
            subprocess.run(self.create_command(name), capture_output=True, check=True, timeout=15)
            code, output, _ = exchange(
                ["docker", "start", "--attach", "--interactive", name],
                project.to_json(),
                seconds=self.seconds,
            )
            if code != 0:
                raise SandboxError(f"worker exited with status {code}")
            payload = unique_json(output)
            if not isinstance(payload, dict):
                raise SandboxError("worker returned an invalid result")
            if set(payload) == {"diagnostics"}:
                if (
                    not isinstance(payload["diagnostics"], list)
                    or not 1 <= len(payload["diagnostics"]) <= 100
                ):
                    raise SandboxError("worker returned invalid diagnostics")
                diagnostics = [Diagnostic.model_validate(d) for d in payload["diagnostics"]]
            elif set(payload) == {"shop"}:
                # Never trust a digest, validation result, or test result from source.
                shop = Shop.model_validate(payload["shop"])
                if shop.id != project.shop_id:
                    raise SandboxError("built shop ID does not match the source project")
                env = self.environment
                artifact = compile_shop(
                    shop,
                    BuildIdentity(
                        source_sha256=project.revision,
                        dependencies_sha256=env.dependencies_sha256,
                        sdk_sha256=env.sdk_sha256,
                        runtime=f"CPython-{env.python_version};docker-{env.image}",
                    ),
                )
                tests = run_cases(artifact, project.tests)
            else:
                raise SandboxError("worker returned an invalid result")
        except ShopValidationError as error:
            diagnostics = list(error.diagnostics[:100])
        except ValidationError as error:
            diagnostics = [
                Diagnostic(
                    code="invalid-field",
                    path=tuple(str(p) for p in item["loc"]),
                    message=item["msg"][:1000],
                )
                for item in error.errors(
                    include_url=False, include_input=False, include_context=False
                )[:100]
            ]
        except (ValueError, OSError, SandboxError, subprocess.SubprocessError) as error:
            diagnostics = [Diagnostic(code="build-failed", path=(), message=str(error)[:1000])]
        finally:
            # Kill container descendants as well as the attached CLI on timeout.
            # Removal failure is a build failure, not an unreported orphan.
            try:
                self.remove(job_id)
            except (OSError, SandboxError, subprocess.SubprocessError):
                diagnostics.append(
                    Diagnostic(
                        code="worker-cleanup-failed",
                        path=(),
                        message=f"Operator must inspect worker container {name}.",
                    )
                )
        return BuildReport(
            source_revision=project.revision,
            worker_image=self.environment.image,
            status="succeeded"
            if artifact and not diagnostics and all(t.passed for t in tests)
            else "failed",
            artifact=artifact,
            diagnostics=tuple(diagnostics),
            tests=tests,
        )
