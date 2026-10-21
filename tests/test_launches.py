"""Creation intent survives expiry, lost replies and competing recovery."""

import json
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chipload.access import ACTIONS, Grant, Principal
from chipload.containers import SandboxError, remove_containers
from chipload.launches import LaunchGuard, LaunchUncertain
from chipload.lifecycle import Conflict, Repository, ShopService
from chipload.project import BuildReport, SourceFile, SourceProject


class LaunchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repository = Repository(Path(temporary.name) / "shop.db")
        service = ShopService(self.repository)
        source = SourceProject(shop_id="example", entrypoint="shop:define", files=(
            SourceFile(path="shop.py", content="def define(): pass"),
        ))
        owner = Principal(subject="owner", grants=(Grant(shop_id="example", actions=ACTIONS),))
        service.save_source(owner, source, expected_head=None)
        self.job = service.request_build(owner, "example", source.revision)
        self.claim, _ = self.repository.claim(self.job.id, 60)
        self.guard = LaunchGuard(self.repository, "builds", self.job.id, self.claim)
        self.name = self.guard.names[0]
        self.command = ["docker", "create", "--name", self.name, "image"]

    def expire(self):
        with self.repository.transaction() as db:
            db.execute("UPDATE builds SET deadline='2000-01-01T00:00:00+00:00' WHERE id=?", (self.job.id,))

    def pending(self):
        with patch("chipload.launches.subprocess.run", side_effect=subprocess.TimeoutExpired("create", 15)):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.guard.create(self.command)

    def test_expired_claim_cannot_initiate_creation(self):
        self.expire()
        self.repository.fence_expired(self.job.id)
        with patch("chipload.launches.subprocess.run") as docker:
            with self.assertRaisesRegex(SandboxError, "no longer active"):
                self.guard.create(self.command)
            docker.assert_not_called()

    def test_only_one_creator_can_attempt_the_same_container(self):
        def create():
            try:
                return self.guard.create(self.command)
            except SandboxError:
                return None

        def docker(*args, **kwargs):
            with self.repository.connection() as db:
                self.assertEqual(db.execute("SELECT settled FROM worker_launches").fetchone()[0], 0)
            return SimpleNamespace(stdout=b"a" * 64)

        with patch("chipload.launches.subprocess.run", side_effect=docker) as run:
            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(lambda _: create(), range(2)))
            self.assertEqual(results.count("a" * 64), 1)
            self.assertEqual(run.call_count, 1)

    def test_missing_container_cannot_settle_a_lost_creation_reply(self):
        self.pending()
        self.expire()
        claim = self.repository.fence_expired(self.job.id)
        guard = LaunchGuard(self.repository, "builds", self.job.id, claim)
        with patch("chipload.launches.subprocess.run", return_value=SimpleNamespace(
            returncode=1, stdout=b"", stderr=b"No such container",
        )) as docker:
            with self.assertRaises(LaunchUncertain):
                remove_containers(self.name, guard=guard)
            self.assertEqual(docker.call_count, 1)
            self.assertEqual(docker.call_args.args[0][1], "container")
        report = BuildReport(source_revision=self.job.source_revision, worker_image="sha256:" + "a" * 64, status="failed")
        with self.assertRaisesRegex(Conflict, "creation is unresolved"):
            self.repository.finish(self.job.id, claim, report)
        self.assertEqual(self.repository.read_quotas()["usage"]["deployment"]["running_builds"], 1)

    def test_observed_creation_is_persisted_before_removal_and_never_replayed(self):
        self.pending()

        def docker(command, **kwargs):
            if command[1] == "container":
                return SimpleNamespace(returncode=0, stderr=b"", stdout="\n".join(
                    json.dumps(value) for value in ("a" * 64, {"org.chipload.worker": "true"}, ["python"], False)
                ).encode())
            with self.repository.connection() as db:
                self.assertEqual(db.execute("SELECT settled FROM worker_launches").fetchone()[0], 1)
            return SimpleNamespace(returncode=0)

        with patch("chipload.launches.subprocess.run", side_effect=docker) as run:
            remove_containers(self.name, guard=self.guard)
            with self.assertRaisesRegex(SandboxError, "already attempted"):
                self.guard.create(self.command)
            self.assertEqual(run.call_count, 2)

    def test_late_acknowledgement_does_not_restore_the_expired_claim(self):
        def docker(*args, **kwargs):
            self.expire()
            self.repository.fence_expired(self.job.id)
            return SimpleNamespace(stdout=b"a" * 64)

        with patch("chipload.launches.subprocess.run", side_effect=docker):
            with self.assertRaisesRegex(SandboxError, "expired during"):
                self.guard.create(self.command)
        with self.repository.connection() as db:
            self.assertEqual(db.execute("SELECT settled FROM worker_launches").fetchone()[0], 1)
        self.assertFalse(self.guard.active())

    def test_schema_seven_migration_retains_unknown_old_running_launches(self):
        with self.repository.transaction() as db:
            db.execute("DROP TABLE launch_reservations")
            db.execute("DROP TABLE worker_launches")
            db.execute("ALTER TABLE python_runs DROP COLUMN kind")
            db.execute("PRAGMA user_version=7")
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda _: Repository(self.repository.path), range(2)))
        with self.repository.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 11)
            row = db.execute("SELECT name,claim,settled FROM worker_launches").fetchone()
            self.assertEqual(tuple(row), (self.name, self.claim, 0))

    def test_failed_schema_migration_rolls_back_launches_and_version(self):
        from chipload import lifecycle

        with self.repository.transaction() as db:
            db.execute("DROP TABLE launch_reservations")
            db.execute("DROP TABLE worker_launches")
            db.execute("ALTER TABLE python_runs DROP COLUMN kind")
            db.execute("PRAGMA user_version=7")
        with patch.object(lifecycle, "LAUNCH_SCHEMA", lifecycle.LAUNCH_SCHEMA + "INVALID SQL;"):
            with self.assertRaises(Exception):
                Repository(self.repository.path)
        with self.repository.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 7)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='worker_launches'").fetchone())
        Repository(self.repository.path)


if __name__ == "__main__":
    unittest.main()
