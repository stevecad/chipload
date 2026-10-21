"""Actual Python execution and hostile programs in disposable Docker containers."""

import os
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from server_support import IdentityFixture

from chipload.lifecycle import BuildProcessor
from chipload.program_model import RunPython
from chipload.program_runner import ProgramProcessor
from chipload.program_sandbox import DockerProgramRunner
from chipload.project import SourceFile
from chipload.sandbox import DockerBuilder, WorkerEnvironment


@unittest.skipUnless(
    os.environ.get("CHIPLOAD_WORKER_TESTS") == "1", "requires a built Docker worker"
)
class ProgramLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = WorkerEnvironment.inspect(
            os.environ.get("CHIPLOAD_WORKER_IMAGE")
            or Path(".local/worker-image").read_text().strip()
        )

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.identity = IdentityFixture(Path(temporary.name) / "data")
        self.addCleanup(self.identity.client.close)
        self.operations = self.identity.operations()
        self.owner = self.identity.settings.bindings[0]
        self.repository = self.operations.service.repository
        self.runner = DockerProgramRunner(self.environment, self.repository)
        self.processor = ProgramProcessor(
            self.repository, self.runner, lambda _: self.owner
        )
        self.service = self.operations.programs

    def run_code(self, code, **kwargs):
        request = RunPython(
            shop_id="thermal-example", request_key=uuid.uuid4().hex, code=code, **kwargs
        )
        job = self.service.submit(self.owner, request)
        self.assertTrue(self.processor.run_next())
        result = self.service.read(self.owner, job.shop_id, job.id)
        self.assertNotEqual(
            subprocess.run(
                ["docker", "inspect", "chipload-code-" + job.id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode,
            0,
        )
        return result

    def test_python_composes_source_build_tests_publication_query_and_rollback(self):
        stopping = threading.Event()
        builder = BuildProcessor(self.repository, DockerBuilder(self.environment))
        errors = []

        def build_loop():
            try:
                while not stopping.wait(0.1):
                    builder.run_next()
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=build_loop)
        thread.start()
        try:
            result = self.run_code(
                """
from helpers import edit
import time
shop=client.shop('thermal-example')
original=shop.publication()
source=shop.source()
saved=shop.save(edit(source['project']), expected_head=source['revision'])
job=shop.build(saved['revision'],request_key='composed-build')
while job['state'] in ('queued','running'):
    time.sleep(.5)
    job=shop.build_result(job['id'])
assert job['state']=='succeeded',job
assert all(case['passed'] for case in job['report']['tests'])
published=shop.publish(job['id'],expected_generation=original['generation'])
result={'name':shop.catalog()['shop']['name'],'tests':len(job['report']['tests']), 'variables':len(shop.variables({})['variables'])}
shop.rollback(original['build_id'],expected_generation=published['generation'])
print('Shop workflow completed')
""",
                files=(
                    SourceFile(
                        path="helpers.py",
                        content="def edit(project):\n    project['files'][0]['content'] += '\\n# program edit\\n'\n    return project\n",
                    ),
                ),
            )
        finally:
            stopping.set()
            thread.join(60)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual(result.state, "succeeded", result)
        self.assertGreater(result.value["tests"], 0)
        self.assertIn("Shop workflow completed", result.logs)
        self.assertTrue(any(call.operation == "publication_change" for call in result.calls))

    def test_container_cannot_access_host_secrets_database_socket_or_network(self):
        result = self.run_code(f"""
import os,socket
from pathlib import Path
assert os.getuid()==65532
for path in ({str(self.repository.path)!r},'/var/run/docker.sock','/etc/chipload/settings.json'):
    assert not Path(path).exists(),path
assert not any(key in os.environ for key in ('CHIPLOAD_CONFIG','AWS_ACCESS_KEY_ID','BAO_TOKEN'))
status=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines())
assert int(status['CapEff'].strip(),16)==0
assert status['NoNewPrivs'].strip()=='1'
assert status['Seccomp'].strip()=='2'
try:
    socket.create_connection(('192.0.2.1',9),timeout=.1)
except OSError:
    pass
else:
    raise AssertionError('network access')
from chipload.client import ServiceError
try:
    client.shop('another-shop').source()
except ServiceError as error:
    assert error.status==403
else:
    raise AssertionError('cross-shop access')
result={{'isolated':True}}
""")
        self.assertEqual(result.state, "succeeded", result)
        self.assertEqual(result.value, {"isolated": True})

    def test_hostile_protocol_timeout_flood_and_process_exit_are_bounded(self):
        for code in (
            "while True: pass",
            "print('x'*100000)",
            "import os; os._exit(0)",
            "client.call('code_run', {})",
            "import sys;sys.__stdout__.write('{bad json}\\n');sys.__stdout__.flush()",
        ):
            with self.subTest(code=code):
                start = time.monotonic()
                result = self.run_code(code, seconds=2)
                self.assertEqual(result.state, "interrupted", result)
                self.assertLess(time.monotonic() - start, 25)

    def test_cancellation_removes_active_container_after_completed_sdk_call(self):
        request = RunPython(
            shop_id="thermal-example",
            request_key="cancel-running",
            code="import time\nclient.shop('thermal-example').source()\ntime.sleep(20)",
        )
        job = self.service.submit(self.owner, request)
        errors = []

        def execute():
            try:
                self.processor.run_next()
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=execute)
        thread.start()
        try:
            for _ in range(100):
                current = self.service.read(self.owner, job.shop_id, job.id)
                if current.calls and current.calls[0].state == "returned":
                    break
                time.sleep(0.1)
            self.assertTrue(current.calls, current)
            self.service.cancel(self.owner, job.shop_id, job.id)
        finally:
            thread.join(40)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors, errors)
        result = self.service.read(self.owner, job.shop_id, job.id)
        self.assertEqual(result.state, "cancelled")
        self.assertEqual(result.calls[0].state, "returned")
        self.assertNotEqual(
            subprocess.run(
                ["docker", "inspect", "chipload-code-" + job.id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode,
            0,
        )

    def test_exception_keeps_committed_source_and_retry_never_executes_again(self):
        result = self.run_code(
            "shop=client.shop('thermal-example')\ns=shop.source()\ns['project']['files'][0]['content']+='\\n# committed before error\\n'\nshop.save(s['project'],expected_head=s['revision'])\nraise RuntimeError('after commit')"
        )
        self.assertEqual(result.state, "failed", result)
        self.assertEqual(result.calls[-1].operation, "source_save")
        self.assertEqual(result.calls[-1].state, "returned")
        source = self.operations.service.get_source(self.owner, "thermal-example")
        self.assertIn("# committed before error", source.project.files[0].content)
        with self.repository.connection() as db:
            request = RunPython.model_validate_json(
                db.execute("SELECT request FROM python_runs WHERE id=?", (result.id,)).fetchone()[0]
            )
        self.assertEqual(self.service.submit(self.owner, request).id, result.id)
        self.assertFalse(self.processor.run_next())
