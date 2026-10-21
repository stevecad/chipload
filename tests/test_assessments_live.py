"""REST and MCP assessments with actual published source, queue and containers."""

import os
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from program_support import wire
from server_support import IdentityFixture

from chipload.access import ACTIONS, Grant, Principal
from chipload.assessment_model import AssessShop
from chipload.auth import TokenVerifier
from chipload.lifecycle import BuildProcessor, Repository, ShopService
from chipload.operations import Operations
from chipload.program_runner import ProgramProcessor
from chipload.program_sandbox import DockerProgramRunner
from chipload.quotas import QuotaPolicy
from chipload.sandbox import DockerBuilder, WorkerEnvironment
from chipload.server import create_app
from examples import python_shop


@unittest.skipUnless(os.environ.get("CHIPLOAD_WORKER_TESTS") == "1", "requires rebuilt worker image")
class AssessmentLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = WorkerEnvironment.inspect(Path(".local/worker-image").read_text().strip())

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.identity = IdentityFixture(Path(temp.name) / "data")
        self.addCleanup(self.identity.client.close)
        self.owner = Principal(subject="owner", grants=tuple(
            Grant(shop_id="python-" + name, actions=ACTIONS) for name in python_shop.PROFILES))
        self.settings = self.identity.settings.model_copy(update={"bindings": (self.owner,)})
        self.repository = Repository(self.settings.database)
        self.service = ShopService(self.repository)
        self.operations = Operations(self.service)
        self.builder = BuildProcessor(self.repository, DockerBuilder(self.environment))
        self.runner = DockerProgramRunner(self.environment, self.repository)
        self.processor = ProgramProcessor(self.repository, self.runner, lambda _: self.owner)

    def publish(self, source):
        try:
            head = self.service.get_source(self.owner, source.shop_id).revision
            generation = self.service.get_publication(self.owner, source.shop_id).generation
        except LookupError:
            head, generation = None, 0
        self.service.save_source(self.owner, source, expected_head=head)
        job = self.service.request_build(self.owner, source.shop_id, source.revision)
        self.builder.run(job.id)
        built = self.service.get_build(self.owner, source.shop_id, job.id)
        self.assertEqual(built.state, "succeeded", str(built.report))
        self.service.publish(self.owner, source.shop_id, job.id, expected_generation=generation)
        return self.service.catalog(source.shop_id)["public_sha256"]

    def query(self, source, revision, *, hostile=False, seconds=30):
        query = source.tests[0].request
        if hostile:
            query = query.model_copy(update={"facts": tuple(
                fact.model_copy(update={"value": python_shop.quantity("999", "mm")})
                if fact.name == "depth" else fact for fact in query.facts)})
        return AssessShop(shop_id=source.shop_id, request_key=uuid.uuid4().hex,
                          expected_revision=revision, query=query, seconds=seconds)

    def removed(self, identifier):
        for prefix in ("code", "sdk"):
            self.assertNotEqual(subprocess.run(
                ["docker", "container", "inspect", f"chipload-{prefix}-{identifier}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode, 0)

    def test_all_python_examples_through_rest_and_actual_mcp_hosted_python(self):
        # This checks domain parity across a batch, independently of container
        # speed. Admission limits have their own deliberately small fixtures.
        quota = self.repository.read_quotas()
        for scope in ("peer", "identity", "mutation_identity", "mutation_shop"):
            quota["policy"]["requests"][scope]["burst"] = 100
        self.repository.set_quotas(
            QuotaPolicy.model_validate(quota["policy"]),
            expected_generation=quota["generation"],
        )
        verifier = TokenVerifier(self.settings, client=self.identity.client)
        app = create_app(self.settings, operations=self.operations, verifier=verifier)
        headers = {"Authorization": "Bearer " + self.identity.token()}
        with TestClient(app, base_url="http://localhost:8088") as http:
            for name in python_shop.PROFILES:
                source = python_shop.project(name)
                revision = self.publish(source)
                for case in source.tests:
                    request = self.query(source, revision).model_copy(update={"query": case.request})
                    path = f"/v1/shops/{source.shop_id}/assessments"
                    queued = http.post(path, json=request.model_dump(mode="json"), headers=headers)
                    self.assertEqual(queued.status_code, 202, queued.text)
                    identifier = queued.json()["id"]
                    self.assertTrue(self.processor.run_next())
                    result = http.get(path + "/" + identifier, headers=headers).json()
                    self.assertEqual(result["state"], "succeeded", result)
                    self.assertEqual(result["evaluation"]["outcome"], case.expected, result)
                    self.removed(identifier)
            # The same typed operation is callable from the existing MCP Python tool.
            submission = {"shop_id": source.shop_id, "request_key": "agent-assess", "code":
                "s=client.shop(inputs['shop_id'])\nresult=s.assess(inputs['query'], expected_revision=s.catalog()['public_sha256'], request_key='from-agent')",
                "inputs": {"shop_id": source.shop_id, "query": source.tests[0].request.model_dump(mode="json")}}
            queued = wire(http, "run_python", submission, headers).json()["result"]["structuredContent"]
            self.processor.run_next()
            agent = self.operations.programs.read(self.owner, source.shop_id, queued["id"])
            self.assertEqual(agent.state, "succeeded", agent)
            assessment = agent.value
            self.processor.run_next()
            read = {"shop_id": source.shop_id, "request_key": "agent-read", "code":
                    "result=client.shop(inputs['shop_id']).assessment(inputs['id'])",
                    "inputs": {"shop_id": source.shop_id, "id": assessment["id"]}}
            queued = wire(http, "run_python", read, headers).json()["result"]["structuredContent"]
            self.processor.run_next()
            agent = self.operations.programs.read(self.owner, source.shop_id, queued["id"])
            self.assertEqual(agent.state, "succeeded", agent)
            expected = http.get(f"/v1/shops/{source.shop_id}/assessments/{assessment['id']}", headers=headers).json()
            self.assertEqual(agent.value, expected)
            self.assertEqual(expected["evaluation"]["outcome"], "supported")

    def hostile_source(self, body):
        source = python_shop.project("machining")
        addition = "\noriginal_fit=machining_fit\ndef machining_fit(facts):\n    if facts['depth'] is not None and facts['depth'].base_value() == 999:\n        " + body + "\n    return original_fit(facts)\n"
        return source.model_copy(update={"files": (source.files[0].model_copy(update={"content": source.files[0].content + addition}),)})

    def test_runtime_exceptions_invalid_answers_timeout_and_output_flood_are_private(self):
        for body in ("raise RuntimeError('private failure text')", "return True", "while True: pass", "print('x'*100000)"):
            with self.subTest(body=body):
                source = self.hostile_source(body)
                revision = self.publish(source)
                job = self.operations.assessments.submit(self.owner, self.query(source, revision, hostile=True, seconds=2))
                self.processor.run_next()
                result = self.operations.assessments.read(self.owner, source.shop_id, job.id)
                self.assertIn(result.state, {"failed", "interrupted"})
                self.assertIsNone(result.evaluation)
                self.assertNotIn("private failure text", result.model_dump_json())
                self.removed(job.id)

    def test_running_cancellation_confirms_worker_removal(self):
        source = self.hostile_source("import time; time.sleep(25)")
        revision = self.publish(source)
        job = self.operations.assessments.submit(self.owner, self.query(source, revision, hostile=True))
        errors = []

        def execute():
            try:
                self.processor.run_next()
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=execute)
        thread.start()
        try:
            running = False
            for _ in range(100):
                state = subprocess.run(["docker", "inspect", "--format", "{{.State.Running}}", "chipload-code-" + job.id], capture_output=True, text=True)
                if state.returncode == 0 and state.stdout.strip() == "true":
                    running = True
                    break
                time.sleep(.1)
            self.assertTrue(running)
            self.operations.assessments.cancel(self.owner, source.shop_id, job.id)
        finally:
            thread.join(40)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual(self.operations.assessments.read(self.owner, source.shop_id, job.id).state, "cancelled")
        self.removed(job.id)

    def test_assessment_source_cannot_access_host_files_database_socket_or_network(self):
        source = python_shop.project("machining")
        addition = f"""
original_fit=machining_fit
def machining_fit(facts):
    import os, socket
    from pathlib import Path
    assert os.getuid()==65532
    assert not Path({str(self.repository.path)!r}).exists()
    assert not Path('/var/run/docker.sock').exists()
    try:
        socket.create_connection(('192.0.2.1',443),timeout=.1)
    except OSError:
        pass
    else:
        raise AssertionError('network access')
    return original_fit(facts)
"""
        source = source.model_copy(update={"files": (source.files[0].model_copy(update={"content": source.files[0].content + addition}),)})
        revision = self.publish(source)
        job = self.operations.assessments.submit(self.owner, self.query(source, revision))
        self.processor.run_next()
        result = self.operations.assessments.read(self.owner, source.shop_id, job.id)
        self.assertEqual(result.state, "succeeded", result)
        self.assertEqual(result.evaluation["outcome"], "supported")
        self.removed(job.id)


if __name__ == "__main__":
    unittest.main()
