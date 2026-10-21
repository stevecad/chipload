"""Durable creation intent fences delayed Docker requests against job recovery."""

import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone

from .containers import SandboxError
from .launch_reservations import inspect_container, is_reservation

LAUNCH_SCHEMA = """
CREATE TABLE worker_launches (
    name TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('builds','python_runs')),
    job_id TEXT NOT NULL,
    claim TEXT NOT NULL,
    settled INTEGER NOT NULL CHECK(settled IN (0,1))
);
CREATE INDEX worker_launches_by_job ON worker_launches(kind,job_id);
"""


class LaunchUncertain(SandboxError):
    """Absence cannot resolve a creation request that may still reach Docker."""


class LaunchGuard:
    def __init__(self, repository, kind, identifier, claim, *, clock=None):
        if kind not in {"builds", "python_runs"} or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("invalid worker job")
        if not isinstance(claim, str) or not re.fullmatch(r"[0-9a-f]{32}", claim):
            raise ValueError("invalid worker claim")
        self.repository, self.kind, self.identifier, self.claim = repository, kind, identifier, claim
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        prefixes = ("build",) if kind == "builds" else ("sdk", "code")
        self.names = tuple(f"chipload-{prefix}-{identifier}" for prefix in prefixes)

    def _active(self, db):
        row = db.execute(f"SELECT * FROM {self.kind} WHERE id=?", (self.identifier,)).fetchone()
        return bool(
            row and row["state"] == "running" and row["claim"] == self.claim
            and row["deadline"] > self.clock().isoformat()
            and (self.kind != "python_runs" or not row["cancel_requested"])
        )

    def active(self):
        with self.repository.connection(timeout=0.1) as db:
            return self._active(db)

    def _settle(self, name):
        # A late acknowledgement remains useful after the job's claim changes.
        # It only resolves this one attempt; it never restores execution rights.
        with self.repository.transaction() as db:
            db.execute(
                "UPDATE worker_launches SET settled=1 WHERE name=? AND NOT EXISTS "
                "(SELECT 1 FROM launch_reservations WHERE name=?)", (name, name),
            )

    def create(self, command):
        name = command[command.index("--name") + 1]
        if command[:2] != ["docker", "create"] or name not in self.names:
            raise ValueError("container does not belong to this job")
        with self.repository.transaction() as db:
            if not self._active(db):
                raise SandboxError("worker claim is no longer active")
            try:
                db.execute("INSERT INTO worker_launches(name,kind,job_id,claim,settled,removal_protocol) VALUES(?,?,?,?,0,2)",
                           (name, self.kind, self.identifier, self.claim))
            except sqlite3.IntegrityError:
                raise SandboxError("container creation was already attempted") from None
        # No database lock is held across Docker. This name gets exactly one
        # creation attempt, even after interruption, expiry or a lost response.
        result = subprocess.run(command, capture_output=True, check=True, timeout=15)
        # A timeout or nonzero CLI exit can leave a request in the daemon; only
        # an acknowledged create or an observed container resolves that intent.
        self._settle(name)
        identifier = result.stdout.decode().strip()
        if not re.fullmatch(r"[0-9a-f]{64}", identifier):
            raise SandboxError("Docker returned an invalid container ID")
        if not self.active():
            raise SandboxError("worker claim expired during container creation")
        return identifier

    def before_remove(self, name, deadline):
        if name not in self.names:
            raise ValueError("container does not belong to this job")
        with self.repository.connection() as db:
            row = db.execute("SELECT settled FROM worker_launches WHERE name=?", (name,)).fetchone()
            reserved = db.execute("SELECT 1 FROM launch_reservations WHERE name=?", (name,)).fetchone()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LaunchUncertain("container creation is unresolved: " + name)
        try:
            found = inspect_container(name, deadline)
        except SandboxError:
            raise LaunchUncertain("container creation is unresolved: " + name) from None
        if reserved:
            if not is_reservation(self.repository, name, found):
                raise LaunchUncertain("container name reservation is not confirmed: " + name)
            with self.repository.transaction() as db:
                db.execute("UPDATE worker_launches SET settled=1 WHERE name=?", (name,))
            return None  # The inert reservation must outlive every delayed request.
        if found is None:
            if row is None or row["settled"]:
                return None
            raise LaunchUncertain("container creation is unresolved: " + name)
        if found["labels"].get("org.chipload.worker") != "true":
            raise LaunchUncertain("unexpected container occupies worker name: " + name)
        # Persist the observation before removal can destroy this evidence.
        if row is not None and not row["settled"]:
            self._settle(name)
        # A concurrent operator may replace this name with a reservation. Never
        # let a delayed removal target that replacement.
        return found["id"]
