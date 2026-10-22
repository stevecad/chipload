"""Actual service subprocesses: authority, state, resource limits and ambiguous commits."""

import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from program_support import wire
from server_support import IdentityFixture

from chipload.access import Principal
from chipload.cache import DataBusy
from chipload.process_operations import ProcessOperations
from chipload.server import create_app
from chipload.service_pool import BOOTSTRAP, MEMORY_BYTES, ServiceFailure, ServicePool


class ServicePoolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.identity = IdentityFixture(self.root)
        self.addCleanup(self.identity.client.close)
        self.original = self.identity.operations()
        self.repository = self.original.service.repository
        self.owner = self.identity.settings.bindings[0]

    def pool(self, *, fault="", **kwargs):
        command = None
        if fault:
            # Operator-owned fixture code simulates implementation faults. There
            # is no fault selector or executable code in the production protocol.
            command = [
                sys.executable,
                "-I",
                "-B",
                "-c",
                BOOTSTRAP.replace(
                    "from chipload.service_worker import main",
                    "exec(sys.argv[3])\nfrom chipload.service_worker import main",
                ),
                str(MEMORY_BYTES),
                str(os.getpid()),
                fault,
            ]
        pool = ServicePool(
            self.repository.path, cache_bytes=32 * 1024**2, command=command, **kwargs
        )
        self.addCleanup(pool.close)
        pool.start()
        return pool

    def test_warm_service_calls_preserve_authority_revisions_and_observations(self):
        pool = self.pool()
        pids = [slot.process.pid for slot in pool.slots]
        body = {"shop_id": "thermal-example"}
        self.assertEqual(
            pool.call("shop_catalog", None, body), self.original.call("shop_catalog", None, body)
        )
        self.assertEqual(
            pool.call("source_read", self.owner, body),
            self.original.call("source_read", self.owner, body),
        )
        for who, status in ((None, 401), (Principal(subject="stranger"), 403)):
            with self.assertRaises(ServiceFailure) as refused:
                pool.call("source_read", who, body)
            self.assertEqual(refused.exception.status, status)
        publication = self.original.call("publication_read", self.owner, body)
        pool.call(
            "publication_change",
            self.owner,
            {**body, "action": "withdraw", "expected_generation": publication["generation"]},
        )
        with self.assertRaises(ServiceFailure) as withdrawn:
            pool.call("shop_catalog", None, body)
        self.assertEqual(withdrawn.exception.status, 404)
        self.assertEqual([slot.process.pid for slot in pool.slots], pids)

    def test_worker_has_memory_cpu_fd_bounds_and_no_parent_environment_or_fds(self):
        descriptor = os.open(self.root / "private-canary", os.O_CREAT | os.O_RDWR, 0o600)
        inherited_fd = fcntl.fcntl(descriptor, fcntl.F_DUPFD, 61)
        os.set_inheritable(inherited_fd, True)
        self.addCleanup(os.close, inherited_fd)
        self.addCleanup(os.close, descriptor)
        fault = """
import os,resource
from chipload.operations import Operations
def information(self,*args):
    try: os.fstat(61); inherited=True
    except OSError: inherited=False
    return {'memory':list(resource.getrlimit(resource.RLIMIT_AS)), 'cpu':list(resource.getrlimit(resource.RLIMIT_CPU)), 'core':list(resource.getrlimit(resource.RLIMIT_CORE)), 'files':list(resource.getrlimit(resource.RLIMIT_NOFILE)), 'canary':os.environ.get('CHIPLOAD_PRIVATE_CANARY'), 'inherited':inherited, 'pid':os.getpid()}
Operations.call=information
"""
        fault = fault.replace("os.fstat(61)", f"os.fstat({inherited_fd})")
        with patch.dict(os.environ, {"CHIPLOAD_PRIVATE_CANARY": "private-test-value"}):
            pool = self.pool(fault=fault)
        result = pool.call("sdk_reference", None, {})
        self.assertEqual(result["memory"], [MEMORY_BYTES] * 2)
        self.assertEqual(result["core"], [0, 0])
        self.assertEqual(result["files"], [64, 64])
        self.assertLessEqual(result["cpu"][0], 4)
        self.assertIsNone(result["canary"])
        self.assertFalse(result["inherited"])
        self.assertNotEqual(result["pid"], os.getpid())

    def test_cpu_memory_wall_time_and_protocol_faults_release_only_dead_workers(self):
        common = "from chipload.operations import Operations\n"
        faults = (
            ("cpu", "def broken(*args):\n    while True: pass\n", 10),
            ("memory", "def broken(*args):\n    return {'data':bytearray(512*1024**2)}\n", 5),
            ("wall", "def broken(*args):\n    import time\n    time.sleep(20)\n", 0.5),
            (
                "protocol",
                "def broken(*args):\n    import sys\n    sys.stdout.write('bad frame\\n');sys.stdout.flush()\n    return {}\n",
                5,
            ),
        )
        for label, code, seconds in faults:
            with self.subTest(fault=label):
                pool = self.pool(fault=common + code + "Operations.call=broken", workers=1)
                pool.seconds = seconds
                process = pool.slots[0].process
                start = time.monotonic()
                with self.assertRaises(ServiceFailure) as interrupted:
                    pool.call("sdk_reference", None, {})
                self.assertEqual(interrupted.exception.status, 503)
                self.assertEqual(interrupted.exception.body["error"]["code"], "service_interrupted")
                self.assertLess(time.monotonic() - start, 7)
                self.assertIsNotNone(process.poll())
                if label == "cpu":
                    self.assertEqual(process.returncode, -signal.SIGXCPU)
                self.assertIsNone(pool.slots[0].process)

    def test_busy_pool_keeps_other_slot_available_and_shutdown_terminates_active_work(self):
        marker = self.root / "running"
        fault = f"""
from chipload.operations import Operations
from pathlib import Path
import time
original=Operations.call
def delayed(self,name,who,request):
    if name=='shop_catalog':
        Path({str(marker)!r}).write_text('running')
        time.sleep(20)
    return original(self,name,who,request)
Operations.call=delayed
"""
        pool = self.pool(fault=fault)
        with ThreadPoolExecutor(2) as threads:
            first = threads.submit(pool.call, "shop_catalog", None, {"shop_id": "thermal-example"})
            for _ in range(100):
                if marker.exists():
                    break
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            self.assertTrue(pool.call("sdk_reference", None, {})["schemas"])
            second = threads.submit(pool.call, "shop_catalog", None, {"shop_id": "thermal-example"})
            for _ in range(100):
                if all(s.busy for s in pool.slots):
                    break
                time.sleep(0.01)
            with self.assertRaises(DataBusy):
                pool.call("sdk_reference", None, {})
            processes = [s.process for s in pool.slots]
            pool.close()
            for job in (first, second):
                with self.assertRaises(ServiceFailure):
                    job.result(timeout=3)
            self.assertTrue(all(p.poll() is not None for p in processes))
        with self.assertRaises(ServiceFailure):
            pool.call("sdk_reference", None, {})

    def test_uncertain_cleanup_keeps_the_slot_unavailable(self):
        fault = "from chipload.operations import Operations\nimport time\ndef stuck(*args):\n    time.sleep(20)\nOperations.call=stuck"
        pool = self.pool(fault=fault, workers=1)
        pool.seconds = 0.1
        with patch("chipload.service_pool.os.killpg"):
            with self.assertRaises(ServiceFailure):
                pool.call("sdk_reference", None, {})
            self.assertTrue(pool.slots[0].poisoned)
            with self.assertRaises(DataBusy):
                pool.call("sdk_reference", None, {})
        pool.close()
        self.assertIsNone(pool.slots[0].process)

    def test_committed_mutation_with_lost_response_is_not_replayed(self):
        marker = self.root / "committed"
        fault = f"""
from chipload.operations import Operations
from pathlib import Path
import os
original=Operations.call
def commit_and_exit(self,name,who,request):
    result=original(self,name,who,request)
    marker=Path({str(marker)!r})
    if name=='build_request' and not marker.exists():
        marker.write_text('committed')
        os._exit(0)
    return result
Operations.call=commit_and_exit
"""
        pool = self.pool(fault=fault, workers=1)
        source = self.original.call("source_read", self.owner, {"shop_id": "thermal-example"})
        request = {
            "shop_id": "thermal-example",
            "revision": source["revision"],
            "request_key": "lost-response",
        }
        with self.assertRaises(ServiceFailure):
            pool.call("build_request", self.owner, request)
        with self.repository.connection() as db:
            first = db.execute(
                "SELECT build_id FROM build_requests WHERE request_key='lost-response'"
            ).fetchone()[0]
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM audit WHERE action='build.requested' AND target=?",
                    (first,),
                ).fetchone()[0],
                1,
            )
        retried = pool.call("build_request", self.owner, request)
        self.assertEqual(retried["id"], first)

    def test_shutdown_attempts_every_worker_when_one_cannot_be_confirmed_dead(self):
        pool = self.pool()
        first, second = [slot.process for slot in pool.slots]
        kill = os.killpg

        def delayed_kill(identifier, sig):
            if identifier != first.pid:
                kill(identifier, sig)

        with patch("chipload.service_pool.os.killpg", side_effect=delayed_kill):
            with self.assertRaises(subprocess.TimeoutExpired):
                pool.close()
        self.assertTrue(pool.slots[0].poisoned)
        self.assertIsNone(pool.slots[1].process)
        self.assertIsNotNone(second.poll())
        pool.close()
        self.assertIsNotNone(first.poll())

    def test_shutdown_does_not_signal_a_process_identifier_already_reaped(self):
        pool = self.pool(workers=1)
        worker = pool.slots[0].process
        worker.terminate()
        worker.wait(timeout=3)
        with patch("chipload.service_pool.os.killpg") as kill:
            pool.close()
        kill.assert_not_called()
        self.assertIsNone(pool.slots[0].process)

    def test_parent_death_terminates_idle_service_processes(self):
        code = """import json,os,sys
from chipload.service_pool import ServicePool
pool=ServicePool(sys.argv[1],cache_bytes=0)
pool.start()
print(json.dumps([slot.process.pid for slot in pool.slots]),flush=True)
os._exit(0)
"""
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code, str(self.repository.path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        children = json.loads(completed.stdout)
        for _ in range(100):
            running = []
            for identifier in children:
                status = Path(f"/proc/{identifier}/status")
                try:
                    state = next(
                        line
                        for line in status.read_text().splitlines()
                        if line.startswith("State:")
                    )
                    if "Z (zombie)" not in state:
                        running.append(identifier)
                except (FileNotFoundError, ProcessLookupError):
                    pass
            if not running:
                break
            time.sleep(0.02)
        self.assertFalse(running, running)

    def test_recycling_and_unexpected_death_do_not_keep_stale_publications(self):
        pool = self.pool(workers=1, max_calls=1)
        body = {"shop_id": "thermal-example"}
        old = pool.slots[0].process
        pool.call("shop_catalog", None, body)
        self.assertEqual(pool.call("identity_read", self.owner, {})["subject"], "owner")
        self.assertIsNotNone(old.poll())
        latest = pool.slots[0].process
        latest.kill()
        latest.wait()
        self.assertEqual(pool.call("shop_catalog", None, body)["shop"]["id"], "thermal-example")
        self.assertNotEqual(latest.pid, pool.slots[0].process.pid)

    def recycling_pool(self):
        block, entered, release = (self.root / name for name in ("block", "entered", "release"))
        fault = f"""
from pathlib import Path
import time
if Path({str(block)!r}).exists():
    Path({str(entered)!r}).touch()
    while not Path({str(release)!r}).exists():
        time.sleep(0.01)
"""
        pool = self.pool(fault=fault, max_calls=1)
        self.addCleanup(release.touch)
        block.touch()
        old = pool.slots[0].process
        self.assertEqual(pool.call("identity_read", self.owner, {})["subject"], "owner")
        for _ in range(200):
            if entered.exists():
                break
            time.sleep(0.01)
        self.assertTrue(entered.exists(), "replacement did not reach its startup gate")
        self.assertIsNotNone(old.poll(), "replacement started before old worker was reaped")
        return pool, release

    def test_retired_worker_warms_without_delaying_other_calls_or_retaining_publication(self):
        pool, release = self.recycling_pool()
        # Startup stays blocked until explicitly released, so a completed call
        # here proves that a warm process served it without waiting for imports.
        with ThreadPoolExecutor(1) as thread:
            publication = thread.submit(
                pool.call, "publication_read", self.owner, {"shop_id": "thermal-example"}
            ).result(timeout=2)
        self.assertFalse(release.exists())
        with self.assertRaises(DataBusy):
            pool.call("identity_read", self.owner, {})
        self.original.call(
            "publication_change",
            self.owner,
            {
                "shop_id": "thermal-example",
                "action": "withdraw",
                "expected_generation": publication["generation"],
            },
        )
        release.touch()
        for _ in range(300):
            if not any(slot.busy for slot in pool.slots):
                break
            time.sleep(0.01)
        self.assertFalse(any(slot.busy for slot in pool.slots))
        with self.assertRaises(ServiceFailure) as withdrawn:
            pool.call("shop_catalog", None, {"shop_id": "thermal-example"})
        self.assertEqual(withdrawn.exception.status, 404)

    def test_shutdown_during_replacement_confirms_children_and_maintenance_terminate(self):
        pool, _ = self.recycling_pool()
        processes = [slot.process for slot in pool.slots]
        maintenance = pool.slots[0].maintenance
        pool.close()
        self.assertFalse(maintenance.is_alive())
        self.assertTrue(all(process.poll() is not None for process in processes))
        self.assertTrue(all(slot.process is None for slot in pool.slots))
        with self.assertRaises(ServiceFailure):
            pool.call("identity_read", self.owner, {})

    def test_unconfirmed_retirement_does_not_block_the_other_worker_or_create_a_replacement(self):
        pool = self.pool(max_calls=1)
        retired = pool.slots[0].process
        attempted = threading.Event()
        kill = os.killpg

        def cannot_stop(pid, sig):
            if pid == retired.pid:
                attempted.set()
            else:
                kill(pid, sig)

        with patch("chipload.service_pool.os.killpg", side_effect=cannot_stop):
            pool.call("identity_read", self.owner, {})
            self.assertTrue(attempted.wait(2))
            with ThreadPoolExecutor(1) as thread:
                result = thread.submit(pool.call, "identity_read", self.owner, {}).result(timeout=2)
            self.assertEqual(result["subject"], "owner")
            for _ in range(600):
                if pool.slots[0].maintenance is None:
                    break
                time.sleep(0.01)
            self.assertIsNone(pool.slots[0].maintenance)
            self.assertTrue(pool.slots[0].poisoned)
            self.assertIs(pool.slots[0].process, retired)
            self.assertIsNone(retired.poll())
        pool.close()
        self.assertIsNotNone(retired.poll())

    def test_failed_background_startup_does_not_fail_a_completed_mutation(self):
        marker = self.root / "bad-startup"
        fault = f"""
from pathlib import Path
import sys,time
if Path({str(marker)!r}).exists():
    sys.stdout.write('bad frame\\n');sys.stdout.flush()
    time.sleep(30)
"""
        pool = self.pool(fault=fault, max_calls=1)
        marker.touch()
        source = self.original.call("source_read", self.owner, {"shop_id": "thermal-example"})
        created = pool.call(
            "build_request",
            self.owner,
            {
                "shop_id": "thermal-example",
                "revision": source["revision"],
                "request_key": "retirement-response",
            },
        )
        for _ in range(300):
            if pool.slots[0].maintenance is None:
                break
            time.sleep(0.01)
        self.assertIsNone(pool.slots[0].process)
        self.assertIsNone(pool.slots[0].maintenance)
        self.assertEqual(
            self.original.call(
                "build_inspect",
                self.owner,
                {
                    "shop_id": "thermal-example",
                    "build_id": created["id"],
                },
            )["id"],
            created["id"],
        )
        with self.repository.connection() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM audit WHERE action='build.requested' AND target=?",
                    (created["id"],),
                ).fetchone()[0],
                1,
            )

    def test_default_rest_and_mcp_use_the_same_process_pool(self):
        app = create_app(self.identity.settings, verifier=self.identity.verifier)
        self.assertIsInstance(app.state.operations, ProcessOperations)
        with TestClient(app, base_url=self.identity.settings.base_url) as client:
            headers = {"Authorization": "Bearer " + self.identity.token()}
            rest = client.get("/v1/shops/thermal-example/catalog")
            mcp = wire(client, "discover_shop", {"shop_id": "thermal-example"}, headers)
            self.assertEqual(rest.json(), mcp.json()["result"]["structuredContent"])
            run = client.post(
                "/v1/shops/thermal-example/runs",
                headers=headers,
                json={"shop_id": "thermal-example", "request_key": "one", "code": "result=1"},
            )
            self.assertEqual(run.status_code, 202, run.text)
            cancelled = wire(
                client,
                "cancel_run",
                {"shop_id": "thermal-example", "run_id": run.json()["id"]},
                headers,
            )
            self.assertEqual(cancelled.json()["result"]["structuredContent"]["state"], "cancelled")
            denied = client.get("/v1/shops/other/source", headers=headers)
            self.assertEqual(denied.status_code, 403)
            pids = [slot.process for slot in app.state.operations.pool.slots]
        self.assertTrue(all(p.poll() is not None for p in pids))
