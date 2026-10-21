"""Operator recovery retains durable name reservations across interruption."""

import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_launches as launch_fixtures

from chipload.artifact import sdk_fingerprint
from chipload.containers import SandboxError, remove_containers
from chipload.launch_reservations import (
    ENTRYPOINT,
    LABEL,
    ensure_reservations,
    recover_launch,
    token,
)
from chipload.lifecycle import Repository
from chipload.sandbox import DockerBuilder, WorkerEnvironment


class Daemon:
    def __init__(self):
        self.objects = {}
        self.creates = 0
        self.lose_reply = False

    def run(self, command, **kwargs):
        if command[1] == "create":
            name = command[command.index("--name") + 1]
            if name in self.objects:
                raise subprocess.CalledProcessError(1, command, stderr=b"Conflict")
            self.creates += 1
            label, value = command[command.index("--label") + 1].split("=", 1)
            self.objects[name] = [f"{self.creates:064x}", {label: value},
                                  [command[command.index("--entrypoint") + 1]], False]
            if self.lose_reply:
                self.lose_reply = False
                raise subprocess.TimeoutExpired(command, 15)
            return SimpleNamespace(returncode=0, stdout=self.objects[name][0].encode(), stderr=b"")
        if command[1] == "container":
            found = self.objects.get(command[-1])
            if found is None:
                return SimpleNamespace(returncode=1, stdout=b"", stderr=b"No such object")
            return SimpleNamespace(returncode=0, stdout="\n".join(map(json.dumps, found)).encode(), stderr=b"")
        if command[1] == "rm":
            for name, found in list(self.objects.items()):
                if found[0] == command[-1]:
                    del self.objects[name]
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        raise AssertionError(command)


class ReservationTests(unittest.TestCase):
    def setUp(self):
        launch_fixtures.LaunchTests.setUp(self)
        launch_fixtures.LaunchTests.pending(self)
        self.builder = DockerBuilder(WorkerEnvironment(
            "sha256:" + "e" * 64, sdk_fingerprint(), "f" * 64, "3.12.15",
        ))
        self.daemon = Daemon()

    def recover(self):
        return recover_launch(self.repository, self.builder, "build", self.job.id)

    def test_recovery_reserves_the_name_and_restore_reinstalls_it(self):
        launch_fixtures.LaunchTests.expire(self)
        with patch("chipload.launch_reservations.subprocess.run", side_effect=self.daemon.run):
            result = self.recover()
            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["reserved_names"], [self.name])
            self.assertEqual(self.daemon.objects[self.name][2], [ENTRYPOINT])
            remove_containers(self.name, guard=self.guard)
            self.assertIn(self.name, self.daemon.objects)
            self.daemon.objects.clear()  # Replacement daemon has no old objects.
            ensure_reservations(self.repository, self.builder)
            self.assertIn(self.name, self.daemon.objects)
            self.assertEqual(self.recover()["state"], "failed")
        self.assertEqual(self.repository.read_quotas()["usage"]["deployment"]["running_builds"], 0)

    def test_lost_reservation_reply_is_resolved_without_repeating_source(self):
        launch_fixtures.LaunchTests.expire(self)
        self.daemon.lose_reply = True
        with patch("chipload.launch_reservations.subprocess.run", side_effect=self.daemon.run):
            self.assertEqual(self.recover()["state"], "failed")
        self.assertEqual(self.daemon.creates, 1)
        self.assertEqual(self.daemon.objects[self.name][1], {LABEL: token(self.repository, self.name)})

    def test_interrupted_operator_attempt_retains_capacity_and_resumes_before_lease_expiry(self):
        launch_fixtures.LaunchTests.expire(self)
        with patch("chipload.launch_reservations.inspect_container", side_effect=SandboxError("daemon unavailable")):
            with self.assertRaises(SandboxError):
                self.recover()
        self.assertEqual(self.repository.read_quotas()["usage"]["deployment"]["running_builds"], 1)
        with self.repository.connection() as db:
            self.assertEqual(db.execute("SELECT settled FROM worker_launches").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM launch_reservations").fetchone()[0], 1)
        with patch("chipload.launch_reservations.subprocess.run", side_effect=self.daemon.run):
            self.assertEqual(self.recover()["state"], "failed")

    def test_foreign_reservation_is_not_removed_or_counted_as_recovery(self):
        launch_fixtures.LaunchTests.expire(self)
        self.daemon.objects[self.name] = ["a" * 64, {LABEL: "another-repository"}, [ENTRYPOINT], False]
        with patch("chipload.launch_reservations.subprocess.run", side_effect=self.daemon.run):
            with self.assertRaisesRegex(SandboxError, "unexpected container"):
                self.recover()
        self.assertEqual(self.daemon.objects[self.name][1][LABEL], "another-repository")
        self.assertEqual(self.repository.read_quotas()["usage"]["deployment"]["running_builds"], 1)

    def test_active_or_legacy_launches_cannot_be_retired(self):
        with patch("chipload.launch_reservations.subprocess.run") as docker:
            with self.assertRaisesRegex(ValueError, "expired"):
                self.recover()
            launch_fixtures.LaunchTests.expire(self)
            with self.repository.transaction() as db:
                db.execute("UPDATE worker_launches SET removal_protocol=1")
            with self.assertRaisesRegex(ValueError, "legacy launch"):
                self.recover()
            docker.assert_not_called()
        with self.repository.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM launch_reservations").fetchone()[0], 0)

    def test_schema_eight_migration_is_atomic_and_preserves_legacy_protocol(self):
        from chipload import lifecycle

        with self.repository.transaction() as db:
            db.execute("DROP TABLE launch_reservations")
            db.execute("ALTER TABLE worker_launches DROP COLUMN removal_protocol")
            db.execute("PRAGMA user_version=8")
        with patch.object(lifecycle, "RESERVATION_SCHEMA", lifecycle.RESERVATION_SCHEMA + "INVALID SQL;"):
            with self.assertRaises(Exception):
                Repository(self.repository.path)
        with self.repository.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 8)
            self.assertNotIn("removal_protocol", [row[1] for row in db.execute("PRAGMA table_info(worker_launches)")])
        Repository(self.repository.path)
        with self.repository.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 10)
            self.assertEqual(db.execute("SELECT settled,removal_protocol FROM worker_launches").fetchone()[:], (0, 1))


if __name__ == "__main__":
    unittest.main()
