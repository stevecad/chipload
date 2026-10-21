"""Actual Docker name reservations, stale creators and operator recovery after restore."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from recovery_support import archive

from chipload.access import ACTIONS, Grant, Principal
from chipload.containers import remove_containers
from chipload.launch_reservations import inspect_container, is_reservation
from chipload.launches import LaunchGuard
from chipload.lifecycle import BuildProcessor, Conflict, Repository, ShopService
from chipload.program_model import RunPython
from chipload.program_runner import ProgramProcessor
from chipload.programs import ProgramService
from chipload.project import SourceFile, SourceProject
from chipload.sandbox import DockerBuilder, WorkerEnvironment


@unittest.skipUnless(os.environ.get("CHIPLOAD_WORKER_TESTS") == "1", "requires a built Docker worker")
class ReservationLiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = Repository(self.root / "data/chipload.db")
        self.environment = WorkerEnvironment.inspect(Path(".local/worker-image").read_text().strip())
        self.builder = DockerBuilder(self.environment, seconds=5)
        self.processor = BuildProcessor(self.repository, self.builder)
        self.service = ShopService(self.repository)
        self.owner = Principal(subject="owner", grants=(Grant(shop_id="example", actions=ACTIONS),))
        self.source = SourceProject(shop_id="example", entrypoint="shop:define", files=(SourceFile(
            path="shop.py", content="from chipload import Shop\ndef define(): return Shop(id='example',name='Example')\n",
        ),))
        self.service.save_source(self.owner, self.source, expected_head=None)
        self.names = []
        self.addCleanup(self.remove_owned_names)

    def remove_owned_names(self):
        # Every delayed creator/remover in these tests is joined before cleanup.
        for name in self.names:
            subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=15)

    def build_job(self):
        job = self.service.request_build(self.owner, "example", self.source.revision)
        self.names.append("chipload-build-" + job.id)
        return job

    def expire(self, kind, identifier):
        with self.repository.transaction() as db:
            db.execute(f"UPDATE {kind} SET deadline='2000-01-01T00:00:00+00:00' WHERE id=?", (identifier,))

    def operator(self, kind, identifier):
        result = subprocess.run(
            [sys.executable, "-m", "chipload.local", "--database", str(self.repository.path),
             "recover-launch", kind, identifier, "--image", self.environment.image],
            capture_output=True, check=True, timeout=60,
        )
        return json.loads(result.stdout)

    def uncertain_build(self, *, actually_create=False):
        job = self.build_job()
        claim, _ = self.repository.claim(job.id, 60)
        guard = LaunchGuard(self.repository, "builds", job.id, claim)
        original = subprocess.run

        def lost(command, **kwargs):
            if actually_create:
                original(command, **kwargs)
            raise subprocess.TimeoutExpired(command, 15)

        with patch("chipload.launches.subprocess.run", side_effect=lost):
            with self.assertRaises(subprocess.TimeoutExpired):
                guard.create(self.builder.create_command(guard.names[0]))
        self.expire("builds", job.id)
        return job, guard

    def test_operator_reservation_blocks_a_paused_creator_and_restores_capacity(self):
        job = self.build_job()
        entered, release = threading.Event(), threading.Event()
        original = subprocess.run
        errors, creates = [], []

        def delayed(command, **kwargs):
            if command[:2] == ["docker", "create"]:
                creates.append(command)
                entered.set()
                if not release.wait(20):
                    raise RuntimeError("creator was not released")
            return original(command, **kwargs)

        def execute():
            try:
                self.processor.run_next()
            except Exception as error:
                errors.append(error)

        with patch("chipload.launches.subprocess.run", side_effect=delayed):
            thread = threading.Thread(target=execute)
            thread.start()
            try:
                self.assertTrue(entered.wait(10))
                self.expire("builds", job.id)
                result = self.operator("build", job.id)
                self.assertEqual(result["state"], "failed")
                self.assertEqual(self.repository.read_quotas()["usage"]["deployment"]["running_builds"], 0)
            finally:
                release.set()
                thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertTrue(all(isinstance(error, Conflict) for error in errors), errors)
        self.assertEqual(len(creates), 1)
        name = "chipload-build-" + job.id
        found = inspect_container(name, time.monotonic() + 5)
        self.assertTrue(is_reservation(self.repository, name, found))
        started = subprocess.run(["docker", "start", found["id"]], capture_output=True, timeout=10)
        self.assertNotEqual(started.returncode, 0)
        self.assertTrue(is_reservation(self.repository, name, inspect_container(name, time.monotonic() + 5)))
        self.assertEqual(self.operator("build", job.id), result)
        next_job = self.build_job()
        self.assertTrue(self.processor.run_next())
        self.assertEqual(self.service.get_build(self.owner, "example", next_job.id).state, "succeeded")

    def test_delayed_cleanup_uses_old_id_and_cannot_remove_the_reservation(self):
        job, guard = self.uncertain_build(actually_create=True)
        entered, release = threading.Event(), threading.Event()
        errors = []
        original = inspect_container

        def delayed(name, deadline):
            found = original(name, deadline)
            entered.set()
            if not release.wait(20):
                raise RuntimeError("cleanup was not released")
            return found

        def cleanup():
            try:
                remove_containers(*guard.names, guard=guard)
            except Exception as error:
                errors.append(error)

        with patch("chipload.launches.inspect_container", side_effect=delayed):
            thread = threading.Thread(target=cleanup)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.operator("build", job.id)["state"], "failed")
            finally:
                release.set()
                thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors, errors)
        self.assertTrue(is_reservation(self.repository, guard.names[0], original(guard.names[0], time.monotonic() + 5)))

    def test_restored_repository_reinstalls_reservations_before_broker_work(self):
        job, guard = self.uncertain_build()
        self.operator("build", job.id)
        before = inspect_container(guard.names[0], time.monotonic() + 5)
        destination = self.root / "restored"
        archive.snapshot_pair(self.repository.path.parent, destination)
        # Model a replacement daemon after all original creators have stopped.
        subprocess.run(["docker", "rm", "--force", before["id"]], capture_output=True, check=True, timeout=15)
        subprocess.run(
            [sys.executable, "-m", "chipload.broker", "--database", str(destination / "chipload.db"),
             "--image", self.environment.image, "--once"],
            capture_output=True, check=True, timeout=30,
        )
        restored = Repository(destination / "chipload.db")
        after = inspect_container(guard.names[0], time.monotonic() + 5)
        self.assertTrue(is_reservation(restored, guard.names[0], after))
        self.assertNotEqual(before["id"], after["id"])
        self.assertEqual(ShopService(restored).get_build(self.owner, "example", job.id).state, "failed")

    def test_operator_recovers_both_python_names_and_preserves_cancellation(self):
        programs = ProgramService(self.repository)
        processor = ProgramProcessor(self.repository, None, lambda _: self.owner)
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                job = programs.submit(self.owner, RunPython(
                    shop_id="example", request_key=uuid.uuid4().hex, code="raise AssertionError('must not execute')",
                ))
                row, _ = processor.claim_next()
                guard = LaunchGuard(self.repository, "python_runs", job.id, row["claim"])
                self.names.extend(guard.names)
                with patch("chipload.launches.subprocess.run", side_effect=subprocess.TimeoutExpired("create", 15)):
                    for name in guard.names:
                        with self.assertRaises(subprocess.TimeoutExpired):
                            guard.create(self.builder.create_command(name))
                if cancel:
                    programs.cancel(self.owner, "example", job.id)
                self.expire("python_runs", job.id)
                outcome = self.operator("python", job.id)
                self.assertEqual(outcome["state"], "cancelled" if cancel else "interrupted")
                record = programs.read(self.owner, "example", job.id)
                self.assertFalse(record.calls)
                self.assertIsNone(record.value)
                remove_containers(*guard.names, guard=guard)
                for name in guard.names:
                    self.assertTrue(is_reservation(self.repository, name, inspect_container(name, time.monotonic() + 5)))


if __name__ == "__main__":
    unittest.main()
