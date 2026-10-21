"""Real SDK service containers: authority, deadlines, uncertain commits and recovery."""

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server_support import IdentityFixture

from chipload.containers import SandboxError
from chipload.lifecycle import now
from chipload.program_model import RunPython
from chipload.program_runner import ProgramProcessor
from chipload.program_sandbox import DockerProgramRunner
from chipload.sdk_service import SDKService


@unittest.skipUnless(
    os.environ.get("CHIPLOAD_WORKER_TESTS") == "1", "requires a built Docker worker"
)
class SDKServiceLiveTests(unittest.TestCase):
    def setUp(self):
        from chipload.sandbox import WorkerEnvironment

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.identity = IdentityFixture(self.root / "data")
        self.addCleanup(self.identity.client.close)
        self.operations = self.identity.operations()
        self.repository = self.operations.service.repository
        self.owner = self.identity.settings.bindings[0]
        self.environment = WorkerEnvironment.inspect(
            Path(".local/worker-image").read_text().strip()
        )
        self.runner = DockerProgramRunner(self.environment, self.repository)
        self.stopping = threading.Event()
        self.processor = ProgramProcessor(
            self.repository, self.runner, lambda _: self.owner, stopping=self.stopping
        )

    def submit(self, code, *, seconds=30):
        request = RunPython(
            shop_id="thermal-example", request_key=uuid.uuid4().hex, code=code, seconds=seconds
        )
        job = self.operations.programs.submit(self.owner, request)
        self.addCleanup(self.runner.remove, job.id)
        return job, request

    def read(self, job):
        return self.operations.programs.read(self.owner, job.shop_id, job.id)

    def assert_removed(self, identifier):
        for prefix in ("chipload-sdk-", "chipload-code-"):
            found = subprocess.run(["docker", "inspect", prefix + identifier], capture_output=True)
            self.assertNotEqual(found.returncode, 0)

    def fault(self, code):
        original = SDKService.command

        def command(service):
            result = original(service)
            result[-2:] = ["-c", code + "\nfrom chipload.sdk_worker import main\nmain()"]
            return result

        return patch.object(SDKService, "command", command)

    def background(self):
        errors = []

        def run():
            try:
                self.processor.run_next()
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        self.addCleanup(thread.join, 40)
        self.addCleanup(self.stopping.set)
        return thread, errors

    def wait_for_call(self, job):
        for _ in range(200):
            record = self.read(job)
            if record.calls:
                return record
            if record.state not in {"queued", "running"}:
                self.fail(str(record))
            time.sleep(0.025)
        self.fail("SDK call did not start")

    def test_actual_service_has_only_data_mount_and_no_socket_settings_or_network(self):
        job, _ = self.submit("import time\nclient.shop('thermal-example').source()\ntime.sleep(20)")
        thread, errors = self.background()
        self.wait_for_call(job)
        container = "chipload-sdk-" + job.id
        info = json.loads(subprocess.check_output(["docker", "inspect", container]))[0]
        host = info["HostConfig"]
        self.assertEqual(host["NetworkMode"], "none")
        self.assertTrue(host["ReadonlyRootfs"])
        self.assertEqual(host["Memory"], 256 * 1024**2)
        self.assertEqual(host["MemorySwap"], host["Memory"])
        self.assertEqual(host["NanoCpus"], 1_000_000_000)
        self.assertEqual(host["CapDrop"], ["ALL"])
        self.assertEqual(info["Config"]["User"], f"{os.getuid()}:{os.getgid()}")
        self.assertEqual(
            [(m["Source"], m["Destination"]) for m in info["Mounts"]],
            [(str(self.repository.path.parent), "/data")],
        )
        code = """
import os,socket
from pathlib import Path
assert os.getuid()!=0
for p in ('/var/run/docker.sock','/etc/chipload/settings.json'):
    assert not Path(p).exists(),p
assert not any(k in os.environ for k in ('BAO_TOKEN','AWS_ACCESS_KEY_ID','CHIPLOAD_CONFIG'))
assert Path('/data/platform.db').is_file()
try: socket.create_connection(('192.0.2.1',9), timeout=.1)
except OSError: pass
else: raise AssertionError('network access')
try: Path('/forbidden').write_text('x')
except OSError: pass
else: raise AssertionError('writable root')
"""
        subprocess.run(["docker", "exec", container, "python", "-I", "-c", code], check=True)
        self.operations.programs.cancel(self.owner, job.shop_id, job.id)
        thread.join(8)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual(self.read(job).state, "cancelled")
        self.assert_removed(job.id)

    def test_service_faults_preserve_started_calls_and_leave_later_runs_usable(self):
        cases = [
            ("cpu", "while True: pass", 30),
            ("memory", "return {'blob':bytearray(512*1024**2)}", 30),
            ("call-wall", "time.sleep(20)", 30),
            ("program-wall", "time.sleep(20)", 1),
            ("protocol", "print('invalid frame',flush=True);time.sleep(20)", 30),
        ]
        for label, body, seconds in cases:
            with self.subTest(fault=label):
                code = (
                    "from chipload.operations import Operations\nimport time\ndef broken(*args):\n    "
                    + body
                    + "\nOperations.call=broken"
                )
                job, request = self.submit(
                    "result=client.shop('thermal-example').source()", seconds=seconds
                )
                durations = []
                original_call = SDKService.call

                def timed(service, *args, **kwargs):
                    start = time.monotonic()
                    try:
                        return original_call(service, *args, **kwargs)
                    finally:
                        durations.append(time.monotonic() - start)

                with self.fault(code), patch.object(SDKService, "call", timed):
                    self.assertTrue(self.processor.run_next())
                # Bound the actual call separately from Docker startup/removal,
                # which have their own timeouts and vary with daemon load.
                self.assertEqual(len(durations), 1)
                self.assertLess(durations[0], 2 if label == "program-wall" else 5.75)
                result = self.read(job)
                self.assertEqual(result.state, "interrupted", result)
                self.assertEqual(result.calls[0].state, "started")
                self.assertEqual(self.operations.programs.submit(self.owner, request), result)
                self.assert_removed(job.id)
        job, _ = self.submit("result=client.shop('thermal-example').source()['revision']")
        self.processor.run_next()
        self.assertEqual(self.read(job).state, "succeeded")

    def test_committed_mutation_with_lost_reply_is_not_replayed(self):
        source = self.operations.service.get_source(self.owner, "thermal-example")
        code = f"result=client.shop('thermal-example').build({source.revision!r}, request_key='sdk-lost-reply')"
        job, request = self.submit(code)
        fault = """
from chipload.operations import Operations
import os
original=Operations.call
def interrupted(self,name,who,request):
    result=original(self,name,who,request)
    if name=='build_request': os._exit(0)
    return result
Operations.call=interrupted
"""
        with self.fault(fault):
            self.processor.run_next()
        result = self.read(job)
        self.assertEqual(result.state, "interrupted")
        self.assertEqual(result.calls[0].state, "started")
        self.assertEqual(self.operations.programs.submit(self.owner, request), result)
        with self.repository.connection() as db:
            rows = db.execute(
                "SELECT build_id FROM build_requests WHERE request_key='sdk-lost-reply'"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        retry, _ = self.submit(code)
        self.processor.run_next()
        self.assertEqual(self.read(retry).value["id"], rows[0]["build_id"])
        self.assert_removed(job.id)

    def test_cancellation_and_broker_stop_interrupt_an_inflight_sdk_call(self):
        fault = "from chipload.operations import Operations\nimport time\ndef blocked(*args):\n    time.sleep(20)\nOperations.call=blocked"
        for action in ("cancel", "stop"):
            with self.subTest(action=action), self.fault(fault):
                self.stopping.clear()
                job, _ = self.submit("result=client.shop('thermal-example').source()")
                thread, errors = self.background()
                self.wait_for_call(job)
                start = time.monotonic()
                if action == "cancel":
                    self.operations.programs.cancel(self.owner, job.shop_id, job.id)
                else:
                    self.stopping.set()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertFalse(errors, errors)
                self.assertLess(time.monotonic() - start, 5)
                self.assertEqual(
                    self.read(job).state, "cancelled" if action == "cancel" else "interrupted"
                )
                self.assertEqual(self.read(job).calls[0].state, "started")
                self.assert_removed(job.id)

    def test_expired_claim_removes_service_before_releasing_run_capacity(self):
        job, _ = self.submit("import time\nclient.shop('thermal-example').source()\ntime.sleep(20)")
        thread, errors = self.background()
        self.wait_for_call(job)
        with self.repository.transaction() as db:
            db.execute(
                "UPDATE python_runs SET deadline=? WHERE id=?",
                ((now() - timedelta(seconds=1)).isoformat(), job.id),
            )
        other = ProgramProcessor(
            self.repository,
            DockerProgramRunner(self.environment, self.repository),
            lambda _: self.owner,
        )
        self.assertEqual(other.recover_expired(), 1)
        thread.join(8)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual(self.read(job).state, "interrupted")
        self.assert_removed(job.id)

    def test_idle_service_exits_without_a_broker_deadline_check(self):
        identifier = uuid.uuid4().hex
        service = SDKService(
            self.runner.builder,
            self.repository.path,
            self.runner.host_data,
            self.repository.admission.identity,
            identifier,
        )
        self.addCleanup(service.close)
        self.addCleanup(self.runner.remove, identifier)
        start = time.monotonic()
        service.start(lambda: True)
        service.child.wait(timeout=65)
        self.assertEqual(service.child.returncode, 124)
        self.assertLess(time.monotonic() - start, 65)

    def test_competing_removal_waits_for_both_actual_containers_to_disappear(self):
        identifier = uuid.uuid4().hex
        names = ("chipload-sdk-" + identifier, "chipload-code-" + identifier)
        self.addCleanup(self.runner.remove, identifier)
        for name in names:
            subprocess.run(self.runner.builder.create_command(name), capture_output=True, check=True, timeout=15)
        original = subprocess.run
        errors = []
        entered = threading.Event()

        def remover():
            entered.wait(5)
            time.sleep(3)  # Keep actual objects present beyond the old two-second check.
            try:
                for name in names:
                    original(["docker", "rm", "--force", name], capture_output=True, check=True, timeout=15)
            except Exception as error:
                errors.append(error)

        def competing(command, **kwargs):
            if command[:3] == ["docker", "rm", "--force"] and command[-1] in names:
                entered.set()
                return SimpleNamespace(returncode=1, stderr=b"removal is already in progress")
            return original(command, **kwargs)

        thread = threading.Thread(target=remover)
        thread.start()
        self.addCleanup(thread.join, 20)
        started = time.monotonic()
        with patch("chipload.containers.subprocess.run", side_effect=competing):
            self.runner.remove(identifier)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors, errors)
        self.assertGreaterEqual(time.monotonic() - started, 3)
        self.assert_removed(identifier)

    def test_recovery_cannot_release_a_run_with_either_container_creation_pending(self):
        for prefix in ("chipload-sdk-", "chipload-code-"):
            with self.subTest(prefix=prefix):
                job, _ = self.submit("result={'must_not_run':True}")
                entered, release = threading.Event(), threading.Event()
                original = subprocess.run

                def expire():
                    with self.repository.transaction() as db:
                        db.execute("UPDATE python_runs SET deadline='2000-01-01T00:00:00+00:00' WHERE id=?", (job.id,))

                def docker(command, **kwargs):
                    if command[:2] == ["docker", "create"] and command[command.index("--name") + 1] == prefix + job.id:
                        entered.set()
                        if not release.wait(20):
                            raise RuntimeError("test did not release creation")
                    return original(command, **kwargs)

                with patch("chipload.launches.subprocess.run", side_effect=docker):
                    thread, errors = self.background()
                    try:
                        self.assertTrue(entered.wait(10))
                        expire()
                        self.assertEqual(self.processor.recover_expired(), 0)
                        self.assertEqual(self.read(job).state, "running")
                        self.assertFalse(self.read(job).calls)
                    finally:
                        release.set()
                        thread.join(30)
                self.assertFalse(thread.is_alive())
                self.assertTrue(all(isinstance(error, SandboxError) for error in errors), errors)
                expire()
                self.assertEqual(self.processor.recover_expired(), 1)
                result = self.read(job)
                self.assertEqual(result.state, "interrupted")
                self.assertIsNone(result.value)
                self.assert_removed(job.id)

    def test_wrong_or_missing_repository_mount_cannot_initialize_or_migrate_storage(self):
        from chipload.lifecycle import Repository

        other = Repository(self.root / "other/platform.db")
        with other.transaction() as db:
            db.execute("PRAGMA user_version=6")
        empty = self.root / "empty"
        empty.mkdir(mode=0o700)
        for directory in (other.path.parent, empty):
            with self.subTest(directory=directory.name):
                identifier = uuid.uuid4().hex
                service = SDKService(
                    self.runner.builder,
                    self.repository.path,
                    directory,
                    self.repository.admission.identity,
                    identifier,
                )
                try:
                    with self.assertRaises((EOFError, ValueError)):
                        service.start(lambda: True)
                finally:
                    self.runner.remove(identifier)
                    service.close()
        with other.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
        self.assertEqual(list(empty.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
