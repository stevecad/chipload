"""Trusted service container for one Python run; database access without Docker authority."""

import os
import stat
import subprocess
import time
from pathlib import Path

from .sandbox import SandboxError
from .service_pool import CPU_SECONDS, WALL_SECONDS, ServiceFailure, ServicePool


def data_directory(database):
    database = Path(database).absolute()
    root = database.parent
    allowed = {
        base + suffix
        for base in (database.name, database.name + ".admission")
        for suffix in ("", "-wal", "-shm", "-journal")
    }
    for path in (root, *root.iterdir()):
        if path != root and path.name not in allowed:
            raise ValueError(
                "SDK calls require a dedicated directory containing only repository files"
            )
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue  # SQLite may remove a journal after the directory listing.
        expected = stat.S_ISDIR if path == root else stat.S_ISREG
        if not expected(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("SDK repository storage must be private and owned by the service user")
    if not database.is_file() or not Path(str(database) + ".admission").is_file():
        raise ValueError("SDK service requires both existing repository databases")
    return root


def local_daemon():
    endpoint = None if os.environ.get("DOCKER_CONTEXT") else os.environ.get("DOCKER_HOST")
    if not endpoint:
        endpoint = subprocess.check_output(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            text=True,
            timeout=15,
        ).strip()
    if not endpoint.startswith("unix:///"):
        raise ValueError("SDK service containers require a local Unix Docker daemon")


class SDKService:
    def __init__(self, builder, database, host_data, identity, identifier):
        self.builder, self.database = builder, Path(database)
        self.host_data, self.identity = Path(host_data), identity
        self.name = "chipload-sdk-" + identifier
        self.child = None

    def command(self):
        command = self.builder.create_command(self.name)
        command[command.index("--user") + 1] = f"{os.getuid()}:{os.getgid()}"
        command[command.index("--ulimit") + 1] = "nofile=64:64"
        # All existing source-worker restrictions remain. This trusted worker
        # adds only repository storage, with no settings, credentials or socket.
        position = command.index(self.builder.environment.image)
        command[position:position] = [
            "--init",
            "--mount",
            f"type=bind,src={self.host_data},dst=/data",
        ]
        command[-1] = "chipload.sdk_worker"
        return command

    def start(self, keep_running):
        if self.child is not None or not keep_running():
            raise SandboxError("SDK service cannot start for an inactive run")
        subprocess.run(self.command(), capture_output=True, check=True, timeout=15)
        self.child = subprocess.Popen(
            ["docker", "start", "--attach", "--interactive", self.name],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        for stream in (self.child.stdin, self.child.stdout):
            os.set_blocking(stream.fileno(), False)
        answer = ServicePool.exchange(
            self.child,
            {
                "database": "/data/" + self.database.name,
                "cache_bytes": 32 * 1024**2,
                "cpu_seconds": CPU_SECONDS,
                "admission_identity": self.identity,
            },
            time.monotonic() + WALL_SECONDS,
            keep_running=keep_running,
        )
        if answer != {"status": 200, "body": {"ready": True}}:
            raise SandboxError("SDK service did not initialize")

    def call(self, operation, principal, request, *, deadline, keep_running):
        if self.child is None:
            raise SandboxError("SDK service is not running")
        try:
            answer = ServicePool.exchange(
                self.child,
                {
                    "operation": operation,
                    "principal": principal.model_dump(mode="json"),
                    "request": request,
                },
                min(deadline, time.monotonic() + WALL_SECONDS),
                keep_running=keep_running,
            )
        except Exception:
            # The caller leaves the durable journal entry as started, then
            # removes both containers before settling the Python run.
            raise SandboxError("SDK service interrupted; a mutation may have committed") from None
        if answer["status"] >= 400:
            raise ServiceFailure(answer["status"], answer["body"])
        return answer["body"]

    def close(self):
        if self.child is not None:
            if self.child.poll() is None:
                self.child.kill()
            self.child.wait(timeout=5)
            self.child.stdin.close()
            self.child.stdout.close()
            self.child = None
