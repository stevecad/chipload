"""Bounded duplex RPC over Docker attach pipes; no service credentials in Python."""

import os
import re
import selectors
import subprocess
import time
from pathlib import Path

from .artifact import canonical_json
from .containers import remove_containers
from .limits import MAX_JSON_BYTES
from .program_model import MAX_PROGRAM_LOG, MAX_PROGRAM_RESULT
from .project import unique_json
from .sandbox import DockerBuilder, SandboxError
from .sdk_service import SDKService, data_directory, local_daemon

MAX_RPC_REQUEST = 3 * 1024**2
MAX_RPC_TOTAL = 32 * 1024**2


def conversation(command, payload, dispatch, keep_running, seconds):
    child = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    pending = canonical_json(payload) + b"\n"
    incoming, errors = bytearray(), bytearray()
    total = len(pending)
    terminal = None
    deadline = time.monotonic() + seconds
    try:
        with selectors.DefaultSelector() as selector:
            for stream, event in (
                (child.stdin, selectors.EVENT_WRITE),
                (child.stdout, selectors.EVENT_READ),
                (child.stderr, selectors.EVENT_READ),
            ):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, event)
            while selector.get_map():
                if time.monotonic() >= deadline or not keep_running():
                    raise SandboxError("Python run interrupted or exceeded its wall-time limit")
                for key, _ in selector.select(min(0.2, max(0, deadline - time.monotonic()))):
                    stream = key.fileobj
                    if stream is child.stdin:
                        sent = os.write(stream.fileno(), pending[:65536])
                        pending = pending[sent:]
                        if not pending:
                            selector.unregister(stream)
                    else:
                        chunk = os.read(stream.fileno(), 65536)
                        if not chunk:
                            selector.unregister(stream)
                            continue
                        if stream is child.stderr:
                            errors.extend(chunk)
                            if len(errors) > MAX_PROGRAM_LOG:
                                raise SandboxError("Python run exceeded its log limit")
                            continue
                        incoming.extend(chunk)
                        total += len(chunk)
                        if len(incoming) > MAX_RPC_REQUEST or total > MAX_RPC_TOTAL:
                            raise SandboxError("Python run exceeded its protocol byte limit")
                        while b"\n" in incoming:
                            line, _, rest = incoming.partition(b"\n")
                            incoming = bytearray(rest)
                            if pending or terminal is not None:
                                raise SandboxError("Python worker sent an out-of-turn message")
                            message = unique_json(bytes(line))
                            if not isinstance(message, dict):
                                raise SandboxError("invalid Python worker message")
                            if message.get("kind") == "call" and set(message) == {
                                "kind",
                                "operation",
                                "request",
                            }:
                                answer = dispatch(
                                    message["operation"], message["request"], deadline=deadline
                                )
                                pending = canonical_json(answer) + b"\n"
                                total += len(pending)
                                if len(pending) > MAX_JSON_BYTES or total > MAX_RPC_TOTAL:
                                    raise SandboxError(
                                        "Python run exceeded its response byte limit"
                                    )
                                selector.register(child.stdin, selectors.EVENT_WRITE)
                            elif message.get("kind") == "result" and set(message) == {
                                "kind",
                                "value",
                            }:
                                if len(canonical_json(message["value"])) > MAX_PROGRAM_RESULT:
                                    raise SandboxError("Python result exceeds its byte limit")
                                terminal = {"value": message["value"]}
                            elif (
                                message.get("kind") == "error"
                                and set(message) == {"kind", "error"}
                                and isinstance(message["error"], str)
                                and len(message["error"]) <= 1000
                            ):
                                terminal = {"error": message["error"]}
                            else:
                                raise SandboxError("invalid Python worker message")
            child.wait(timeout=max(0.001, deadline - time.monotonic()))
            if child.returncode != 0 or terminal is None or incoming or pending:
                raise SandboxError("Python worker exited without a complete result")
            return {**terminal, "logs": errors.decode("utf-8", errors="replace")}
    except subprocess.TimeoutExpired:
        raise SandboxError("Python worker exceeded its wall-time limit") from None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        child.stdin.close()
        child.stdout.close()
        child.stderr.close()


class DockerProgramRunner:
    def __init__(self, environment, repository, *, host_data=None):
        if os.getuid() == 0:
            raise ValueError("run the Python broker as the non-root repository owner")
        self.builder = DockerBuilder(environment)
        self.environment = environment
        self.database, self.identity = repository.path, repository.admission.identity
        root = data_directory(self.database)
        self.host_data = Path(host_data).absolute() if host_data else root
        if self.host_data == Path("/") or any(c in str(self.host_data) for c in (",", "\n", "\r")):
            raise ValueError("invalid SDK service host data directory")
        local_daemon()
        self.services = {}

    def remove(self, identifier, *, guard=None):
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("invalid Python run ID")
        remove_containers("chipload-sdk-" + identifier, "chipload-code-" + identifier, guard=guard)

    def service_call(self, identifier, operation, principal, request, *, deadline, keep_running):
        service = self.services.get(identifier)
        if service is None:
            raise SandboxError("SDK service is not attached to this run")
        return service.call(
            operation, principal, request, deadline=deadline, keep_running=keep_running
        )

    def run(self, identifier, request, dispatch, keep_running, *, guard=None):
        if not re.fullmatch(r"[0-9a-f]{32}", identifier) or identifier in self.services:
            raise ValueError("invalid or active Python run ID")
        name = "chipload-code-" + identifier
        command = self.builder.create_command(name)
        command[-1] = "chipload.program_worker"
        service = SDKService(self.builder, self.database, self.host_data, self.identity, identifier)
        self.services[identifier] = service
        try:
            service.start(keep_running, guard=guard)
            if not keep_running():
                raise SandboxError("Python run is no longer active")
            if guard is None:
                subprocess.run(command, capture_output=True, check=True, timeout=15)
                container = name
            else:
                container = guard.create(command)
            return conversation(
                ["docker", "start", "--attach", "--interactive", container],
                request.model_dump(mode="json"),
                dispatch,
                keep_running,
                request.seconds,
            )
        finally:
            try:
                self.remove(identifier, guard=guard)
            finally:
                try:
                    service.close()
                finally:
                    self.services.pop(identifier, None)
