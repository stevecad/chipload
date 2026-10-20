"""Bounded duplex RPC over Docker attach pipes; no service credentials in Python."""

import os
import re
import selectors
import subprocess
import time

from .artifact import canonical_json
from .limits import MAX_JSON_BYTES
from .program_model import MAX_PROGRAM_LOG, MAX_PROGRAM_RESULT
from .project import unique_json
from .sandbox import DockerBuilder, SandboxError

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
                                answer = dispatch(message["operation"], message["request"])
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
    def __init__(self, environment):
        self.builder = DockerBuilder(environment)
        self.environment = environment

    def remove(self, identifier):
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("invalid Python run ID")
        result = subprocess.run(
            ["docker", "rm", "--force", "chipload-code-" + identifier],
            capture_output=True,
            timeout=15,
        )
        if result.returncode and b"No such container" not in result.stderr:
            raise SandboxError("could not remove Python worker")

    def run(self, identifier, request, dispatch, keep_running):
        name = "chipload-code-" + identifier
        command = self.builder.create_command(name)
        command[-1] = "chipload.program_worker"
        try:
            subprocess.run(command, capture_output=True, check=True, timeout=15)
            return conversation(
                ["docker", "start", "--attach", "--interactive", name],
                request.model_dump(mode="json"),
                dispatch,
                keep_running,
                request.seconds,
            )
        finally:
            self.remove(identifier)
