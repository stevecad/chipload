"""Operator-owned inert containers reserve names against delayed worker creation."""

import hashlib
import json
import re
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone

from .containers import SandboxError, _absent, _remove

RESERVATION_SCHEMA = """
ALTER TABLE worker_launches ADD COLUMN removal_protocol INTEGER NOT NULL DEFAULT 1
    CHECK(removal_protocol IN (1,2));
CREATE TABLE launch_reservations (
    name TEXT PRIMARY KEY REFERENCES worker_launches(name),
    created_at TEXT NOT NULL
);
"""
LABEL = "org.chipload.launch-reservation"
ENTRYPOINT = "/"  # A directory cannot execute, regardless of image contents.


def token(repository, name):
    return hashlib.sha256((repository.admission.identity + "\0" + name).encode()).hexdigest()


def inspect_container(name, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SandboxError("container inspection deadline expired")
    try:
        result = subprocess.run(
            ["docker", "container", "inspect", "--format",
             "{{json .Id}}\n{{json .Config.Labels}}\n{{json .Config.Entrypoint}}\n{{json .State.Running}}", name],
            capture_output=True, timeout=min(1, remaining),
        )
        if _absent(result):
            return None
        if result.returncode or len(result.stdout) > 8192:
            raise ValueError("invalid container inspection")
        identifier, labels, entrypoint, running = map(json.loads, result.stdout.splitlines())
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier):
            raise ValueError("invalid container ID")
        if not isinstance(labels, dict) or not isinstance(running, bool):
            raise ValueError("invalid container metadata")
        return {"id": identifier, "labels": labels, "entrypoint": entrypoint, "running": running}
    except (OSError, ValueError, subprocess.SubprocessError):
        raise SandboxError("could not inspect worker container " + name) from None


def is_reservation(repository, name, found):
    return bool(
        found and found["labels"].get(LABEL) == token(repository, name)
        and found["labels"].get("org.chipload.worker") != "true"
        and found["entrypoint"] == [ENTRYPOINT] and not found["running"]
    )


def install_reservation(repository, builder, name):
    from .launches import LaunchGuard

    with repository.connection() as db:
        record = db.execute(
            "SELECT w.* FROM worker_launches w JOIN launch_reservations r ON w.name=r.name WHERE w.name=?",
            (name,),
        ).fetchone()
    if record is None or record["removal_protocol"] != 2:
        raise ValueError("missing or incompatible launch reservation")
    guard = LaunchGuard(repository, record["kind"], record["job_id"], record["claim"])
    if name not in guard.names:
        raise ValueError("invalid reserved container name")
    deadline = time.monotonic() + 45
    for _ in range(3):
        found = inspect_container(name, deadline)
        if is_reservation(repository, name, found):
            with repository.transaction() as db:
                db.execute("UPDATE worker_launches SET settled=1 WHERE name=?", (name,))
            return
        if found is not None:
            if found["labels"].get("org.chipload.worker") != "true":
                raise SandboxError("unexpected container occupies reserved name: " + name)
            if not _remove(found["id"], min(deadline, time.monotonic() + 20)):
                raise SandboxError("could not remove interrupted worker " + name)
        command = builder.create_command(name)
        command[command.index("--entrypoint") + 1] = ENTRYPOINT
        command[command.index("--label") + 1] = LABEL + "=" + token(repository, name)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            subprocess.run(command, capture_output=True, check=True, timeout=min(15, remaining))
        except (OSError, subprocess.SubprocessError):
            pass  # Resolve a lost reply or name collision by inspection.
    found = inspect_container(name, deadline)
    if not is_reservation(repository, name, found):
        raise SandboxError("could not confirm worker name reservation " + name)
    with repository.transaction() as db:
        db.execute("UPDATE worker_launches SET settled=1 WHERE name=?", (name,))


def ensure_reservations(repository, builder):
    """Reinstall durable reservations before a broker admits execution after restore."""
    with repository.connection() as db:
        names = [row[0] for row in db.execute("SELECT name FROM launch_reservations ORDER BY name")]
    for name in names:
        install_reservation(repository, builder, name)


def recover_launch(repository, builder, kind, identifier):
    """Local operator action; no shop grant or remote operation exposes it."""
    from .launches import LaunchGuard
    from .lifecycle import Conflict

    table = {"build": "builds", "python": "python_runs"}.get(kind)
    if table is None or not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise ValueError("invalid worker job")
    at = datetime.now(timezone.utc)
    claim = uuid.uuid4().hex
    with repository.transaction() as db:
        job = db.execute(f"SELECT * FROM {table} WHERE id=?", (identifier,)).fetchone()
        if job is None:
            raise ValueError("worker job not found")
        rows = db.execute(
            "SELECT w.*,r.name AS reserved FROM worker_launches w LEFT JOIN launch_reservations r "
            "ON w.name=r.name WHERE w.kind=? AND w.job_id=? AND (w.settled=0 OR r.name IS NOT NULL)",
            (table, identifier),
        ).fetchall()
        if not rows:
            raise ValueError("job has no unresolved or reserved launches")
        if any(row["removal_protocol"] != 2 for row in rows):
            raise ValueError("legacy launch may have delayed cleanup by name; operator maintenance is required")
        resuming = all(row["reserved"] for row in rows)
        if not resuming and (job["state"] != "running" or job["deadline"] > at.isoformat()):
            raise ValueError("only expired running jobs can reserve new container names")
        terminal = job["state"] != "running"
        if not terminal:
            db.execute(f"UPDATE {table} SET claim=?,deadline=? WHERE id=?",
                       (claim, (at + timedelta(seconds=120)).isoformat(), identifier))
        for row in rows:
            if not row["reserved"]:
                db.execute("INSERT INTO launch_reservations VALUES(?,?)", (row["name"], at.isoformat()))
                repository.event(db, job["shop_id"], "local-operator", "worker.reserve", row["name"])
    names = [row["name"] for row in rows]
    for name in names:
        install_reservation(repository, builder, name)
    if not terminal:
        from .containers import remove_containers
        from .project import BuildReport
        from .validation import Diagnostic

        guard = LaunchGuard(repository, table, identifier, claim)
        remove_containers(*guard.names, guard=guard)
        try:
            if table == "builds":
                repository.finish(identifier, claim, BuildReport(
                    source_revision=job["source_revision"], worker_image=builder.environment.image,
                    status="failed", diagnostics=(Diagnostic(code="worker-interrupted", path=(),
                        message="Operator reserved unresolved worker creation; source was not replayed."),),
                ))
            else:
                from .program_runner import ProgramProcessor

                ProgramProcessor(repository, None, lambda _: None).finish(
                    dict(job) | {"claim": claim},
                    {"error": "Operator recovered unresolved creation; recorded calls may have committed"},
                    "interrupted",
                )
        except Conflict:
            pass  # Another recovery owns completion; reservations remain in place.
    with repository.connection() as db:
        state = db.execute(f"SELECT state FROM {table} WHERE id=?", (identifier,)).fetchone()[0]
    return {"id": identifier, "state": state, "reserved_names": names}
