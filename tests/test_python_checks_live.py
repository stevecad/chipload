"""Real contained Python checks and trusted composition of their answers."""

import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from chipload.access import ACTIONS, Grant, Principal
from chipload.lifecycle import BuildProcessor, Repository, ShopService
from chipload.published import PublishedService
from chipload.sandbox import DockerBuilder, WorkerEnvironment
from examples.python_shop import AT, PROFILES, project


@unittest.skipUnless(
    os.environ.get("CHIPLOAD_WORKER_TESTS") == "1", "requires a built Docker worker"
)
class PythonCheckLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = WorkerEnvironment.inspect(Path(".local/worker-image").read_text().strip())

    def amended(self, suffix):
        source = project("machining")
        file = source.files[0]
        return source.model_copy(
            update={"files": (file.model_copy(update={"content": file.content + suffix}),)}
        )

    def build(self, source, seconds=10):
        identifier = uuid.uuid4().hex
        report = DockerBuilder(self.environment, seconds=seconds).build(source, job_id=identifier)
        found = subprocess.run(
            ["docker", "container", "inspect", "chipload-build-" + identifier],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.assertNotEqual(found.returncode, 0, "check worker was left behind")
        return report

    def test_agent_builds_tests_publishes_and_inspects_each_python_shop(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "shop.db")
            service = ShopService(repository)
            owner = Principal(
                subject="owner",
                grants=tuple(
                    Grant(shop_id="python-" + profile, actions=ACTIONS) for profile in PROFILES
                ),
            )
            builder = DockerBuilder(self.environment)
            for profile in PROFILES:
                source = project(profile)
                service.save_source(owner, source, expected_head=None)
                job = service.request_build(owner, source.shop_id, source.revision)
                BuildProcessor(repository, builder).run(job.id)
                report = service.get_build(owner, source.shop_id, job.id).report
                self.assertEqual(report.status, "succeeded", str(report))
                self.assertEqual(len(report.tests), 10)
                self.assertTrue(all(case.passed for case in report.tests))
                for case in report.tests:
                    self.assertEqual(
                        case.python_evaluation.build_sha256, report.artifact.build_sha256
                    )
                    self.assertEqual(len(case.python_evaluation.results), 2)
                service.publish(owner, source.shop_id, job.id, expected_generation=0)
                # Until an on-demand assessment has run, these build-test answers
                # cannot be used as answers to a new published query.
                published = PublishedService(service, clock=lambda: AT)
                self.assertEqual(
                    published.evaluation(source.shop_id, source.tests[0].request)["outcome"],
                    "unknown",
                )
            repeated = self.build(source)
            self.assertEqual(repeated.artifact.build_sha256, report.artifact.build_sha256)
            self.assertEqual(repeated.tests, report.tests)

    def test_source_cannot_override_static_constraints_or_evidence(self):
        source = self.amended(
            "\ndef machining_fit(facts): return 'supported'\ndef machining_business(facts): return 'supported'\n"
        )
        report = self.build(source)
        self.assertEqual(report.status, "failed")
        cases = {case.id: case for case in report.tests}
        self.assertEqual(cases["static-limit"].actual, "unsupported")
        self.assertEqual(cases["qualification-missing"].actual, "unknown")
        self.assertFalse(cases["relationship-rejected"].passed)

    def test_invalid_exception_loop_and_output_fail_as_jobs_not_domain_answers(self):
        bodies = (
            "return True",
            "raise RuntimeError('broken check')",
            "while True: pass",
            "print('x' * 100000); return 'supported'",
        )
        for body in bodies:
            with self.subTest(body=body):
                report = self.build(
                    self.amended("\ndef machining_fit(facts):\n    " + body + "\n"), seconds=2
                )
                self.assertEqual(report.status, "failed")
                self.assertTrue(report.diagnostics)
                if body in bodies[:2]:
                    self.assertEqual(report.diagnostics[0].code, "python-check-error")
                    self.assertEqual(
                        report.diagnostics[0].path,
                        ("tests", "supported", "python_checks", "service", "relationship"),
                    )
        self.assertEqual(self.build(project("machining")).status, "succeeded")

    def test_check_function_has_no_host_files_network_credentials_or_docker(self):
        with tempfile.NamedTemporaryFile(prefix="chipload-check-canary-") as canary:
            suffix = f"""
original_fit = machining_fit
def machining_fit(facts):
    import os, socket
    from pathlib import Path
    assert os.getuid() == 65532
    assert not Path({canary.name!r}).exists()
    assert not Path('/var/run/docker.sock').exists()
    assert os.environ.get('CHIPLOAD_CHECK_CANARY') is None
    try:
        socket.create_connection(('192.0.2.1', 443), timeout=0.1)
    except OSError:
        pass
    else:
        raise AssertionError('network available')
    return original_fit(facts)
"""
            with patch.dict(os.environ, {"CHIPLOAD_CHECK_CANARY": "private"}):
                report = self.build(self.amended(suffix))
            self.assertEqual(report.status, "succeeded", str(report))


if __name__ == "__main__":
    unittest.main()
