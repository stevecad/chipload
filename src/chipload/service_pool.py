"""Bounded persistent service processes; no request or mutation is replayed."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field

from .artifact import canonical_json
from .cache import DataBusy
from .limits import bounded_json
from .model import Model

MAX_INPUT = 4 * 1024**2
MAX_OUTPUT = 8 * 1024**2
WORKERS = 2
MEMORY_BYTES = 256 * 1024**2
CPU_SECONDS = 2
WALL_SECONDS = 5
MAX_CALLS = 1000

# Limits apply before importing the SDK. Never use preexec_fn in a threaded server.
BOOTSTRAP = """import resource,sys,os,select,threading,ctypes
memory=int(sys.argv[1])
resource.setrlimit(resource.RLIMIT_AS,(memory,memory))
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
hard=resource.getrlimit(resource.RLIMIT_CPU)[1]
resource.setrlimit(resource.RLIMIT_CPU,(min(3,hard) if hard>=0 else 3,hard))
resource.setrlimit(resource.RLIMIT_NOFILE,(64,64))
def parent_guard():
    try:
        parent=int(sys.argv[2])
        libc=ctypes.CDLL(None,use_errno=True)
        descriptor=libc.pidfd_open(parent,0)
        if descriptor<0:
            os._exit(1)
        if os.getppid()!=parent:
            os._exit(1)
        select.select([descriptor],[],[])
    except BaseException:
        pass
    os.killpg(os.getpid(),9)
    os._exit(1)
threading.Thread(target=parent_guard,daemon=True).start()
from chipload.service_worker import main
main()
"""


class ServiceFailure(Exception):
    """A child's structured refusal, or an ambiguous interrupted service call."""

    def __init__(self, status, body):
        self.status, self.body = status, body
        self.retry_after = body.get("error", {}).get("retry_after")
        super().__init__("service request could not be completed")


def unavailable():
    return ServiceFailure(
        503,
        {
            "error": {
                "code": "service_interrupted",
                "message": "Service worker interrupted; a submitted mutation may have committed. Inspect its durable record before retrying.",
                "retry_after": 1,
            }
        },
    )


@dataclass
class Slot:
    process: subprocess.Popen | None = None
    busy: bool = False
    calls: int = 0
    poisoned: bool = False
    guard: threading.Lock = field(default_factory=threading.Lock)


class ServicePool:
    def __init__(
        self,
        database,
        *,
        cache_bytes,
        workers=WORKERS,
        seconds=WALL_SECONDS,
        memory_bytes=MEMORY_BYTES,
        cpu_seconds=CPU_SECONDS,
        max_calls=MAX_CALLS,
        command=None,
    ):
        if (
            not 1 <= workers <= 4
            or not 0 < seconds <= 30
            or not 1 <= cpu_seconds <= 10
            or max_calls < 1
        ):
            raise ValueError("invalid service process limits")
        self.configuration = {
            "database": str(database),
            "cache_bytes": cache_bytes // workers,
            "cpu_seconds": cpu_seconds,
        }
        self.seconds, self.memory_bytes, self.max_calls = seconds, memory_bytes, max_calls
        self.command = command or [
            sys.executable,
            "-I",
            "-B",
            "-c",
            BOOTSTRAP,
            str(memory_bytes),
            str(os.getpid()),
        ]
        self.slots = [Slot() for _ in range(workers)]
        self.lock = threading.Lock()
        self.closed = False

    @staticmethod
    def stop(slot):
        with slot.guard:
            process = slot.process
            if process is None:
                return
            slot.poisoned = True
            # A slot is reusable only after termination has been confirmed.
            # Once reaped, its numeric PID may belong to an unrelated process.
            # Polling and signaling share this guard to prevent that race.
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=2)
            process.stdin.close()
            process.stdout.close()
            slot.process, slot.calls, slot.poisoned = None, 0, False

    @staticmethod
    def dead(slot):
        with slot.guard:
            return slot.process is None or slot.process.poll() is not None

    @staticmethod
    def exchange(process, message, deadline, *, keep_running=None):
        identifier = uuid.uuid4().hex
        payload = canonical_json({"id": identifier, "body": message}) + b"\n"
        if len(payload) > MAX_INPUT:
            raise ValueError("service request exceeds its IPC bound")
        incoming = bytearray()
        position = 0
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdin, selectors.EVENT_WRITE)
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                if keep_running is not None and not keep_running():
                    raise InterruptedError("service call cancelled or fenced")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                for key, _ in selector.select(min(remaining, 0.2)):
                    if key.fileobj is process.stdin:
                        position += os.write(
                            process.stdin.fileno(), payload[position : position + 65536]
                        )
                        if position == len(payload):
                            selector.unregister(process.stdin)
                    else:
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            raise EOFError()
                        incoming.extend(chunk)
                        if len(incoming) > MAX_OUTPUT:
                            raise ValueError("service response exceeds its IPC bound")
                        if b"\n" in chunk:
                            if (
                                not incoming.endswith(b"\n")
                                or incoming.count(b"\n") != 1
                                or position != len(payload)
                            ):
                                raise ValueError("invalid service protocol")
                            answer = bounded_json(bytes(incoming), max_bytes=MAX_OUTPUT)
                            if (
                                not isinstance(answer, dict)
                                or set(answer) != {"id", "status", "body"}
                                or answer["id"] != identifier
                                or type(answer["status"]) is not int
                                or not 200 <= answer["status"] <= 599
                                or not isinstance(answer["body"], dict)
                            ):
                                raise ValueError("invalid service response")
                            return {"status": answer["status"], "body": answer["body"]}

    def spawn(self, slot, deadline):
        with self.lock:
            if self.closed:
                raise unavailable()
            slot.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
                cwd="/",
                env={"LANG": "C.UTF-8"},
            )
        for stream in (slot.process.stdin, slot.process.stdout):
            os.set_blocking(stream.fileno(), False)
        answer = self.exchange(slot.process, self.configuration, deadline)
        if answer != {"status": 200, "body": {"ready": True}}:
            raise ValueError("service worker did not initialize")

    def start(self):
        # Startup warms imports without exposing a request to import latency.
        for slot in self.slots:
            try:
                self.spawn(slot, time.monotonic() + self.seconds)
            except BaseException:
                self.close()
                raise unavailable() from None

    def close(self):
        with self.lock:
            self.closed = True
            failure = None
            for slot in self.slots:
                try:
                    self.stop(slot)
                except (OSError, subprocess.TimeoutExpired) as error:
                    failure = error
            if failure is not None:
                raise failure

    def call(self, operation, principal, request):
        payload = {
            "operation": operation,
            "principal": principal.model_dump(mode="json") if principal else None,
            "request": request.model_dump(mode="json") if isinstance(request, Model) else request,
        }
        with self.lock:
            if self.closed:
                raise unavailable()
            for candidate in self.slots:
                if candidate.poisoned and self.dead(candidate):
                    self.stop(candidate)
            slot = next((slot for slot in self.slots if not slot.busy and not slot.poisoned), None)
            if slot is None:
                raise DataBusy("service workers are at capacity; retry later")
            slot.busy = True
        deadline = time.monotonic() + self.seconds
        try:
            if self.dead(slot) or slot.calls >= self.max_calls:
                self.stop(slot)
                self.spawn(slot, deadline)
            answer = self.exchange(slot.process, payload, deadline)
            slot.calls += 1
            if answer["status"] >= 400:
                raise ServiceFailure(answer["status"], answer["body"])
            return answer["body"]
        except ServiceFailure:
            raise
        except BaseException as error:
            try:
                self.stop(slot)
            except (OSError, subprocess.TimeoutExpired):
                pass  # Keep the poisoned slot unavailable until termination is observed.
            if not isinstance(error, Exception):
                raise
            raise unavailable() from None
        finally:
            with self.lock:
                slot.busy = False
