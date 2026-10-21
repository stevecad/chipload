"""Actual isolated Python builds of alternative-route shop projects."""

import os
import tempfile
import unittest
import uuid
from pathlib import Path

from chipload.access import ACTIONS, Grant, Principal
from chipload.lifecycle import BuildProcessor, Repository, ShopService
from chipload.operations import Operations
from chipload.program_model import RunPython
from chipload.program_runner import ProgramProcessor
from chipload.program_sandbox import DockerProgramRunner
from chipload.sandbox import DockerBuilder, WorkerEnvironment
from examples.alternative_routes import PROFILES, project


@unittest.skipUnless(
    os.environ.get("CHIPLOAD_WORKER_TESTS") == "1", "requires a built Docker worker"
)
class OfferWorkerLiveTests(unittest.TestCase):
    def test_exported_manufacturing_thermal_and_service_projects_build_and_pass_cases(self):
        builder = DockerBuilder(
            WorkerEnvironment.inspect(Path(".local/worker-image").read_text().strip())
        )
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "data/shop.db")
            service = ShopService(repository)
            operations = Operations(service)
            owner = Principal(
                subject="owner",
                grants=tuple(
                    Grant(shop_id="routes-" + profile, actions=ACTIONS) for profile in PROFILES
                ),
            )
            runner = DockerProgramRunner(builder.environment, repository)
            processor = ProgramProcessor(repository, runner, lambda _: owner)
            for profile in PROFILES:
                source = project(profile)
                service.save_source(owner, source, expected_head=None)
                build = service.request_build(owner, source.shop_id, source.revision)
                BuildProcessor(repository, builder).run(build.id)
                report = service.get_build(owner, source.shop_id, build.id).report
                self.assertEqual(report.status, "succeeded", report.model_dump_json())
                self.assertEqual(len(report.tests), 8)
                self.assertTrue(all(case.passed for case in report.tests))
                self.assertEqual(report.artifact.shop.offers[0].id, "service")
                self.assertEqual(report.artifact.identity.source_sha256, source.revision)
                service.publish(owner, source.shop_id, build.id, expected_generation=0)
                request = RunPython(
                    shop_id=source.shop_id,
                    request_key=uuid.uuid4().hex,
                    code="shop = client.shop(inputs['shop_id'])\nresult = [shop.evaluate(query)['outcome'] for query in inputs['queries']]",
                    inputs={
                        "shop_id": source.shop_id,
                        "queries": [case.request.model_dump(mode="json") for case in source.tests],
                    },
                )
                run = operations.programs.submit(owner, request)
                try:
                    self.assertTrue(processor.run_next())
                    result = operations.programs.read(owner, source.shop_id, run.id)
                    self.assertEqual(result.state, "succeeded", str(result))
                    self.assertEqual(result.value, [case.expected for case in source.tests])
                finally:
                    runner.remove(run.id)
