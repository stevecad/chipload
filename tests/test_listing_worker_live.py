"""Exported supplier projects and listing queries in actual isolated Python workers."""

import os
import tempfile
import unittest
from pathlib import Path

from chipload.access import ACTIONS, Grant, Principal
from chipload.lifecycle import BuildProcessor, Repository, ShopService
from chipload.operations import Operations
from chipload.program_model import RunPython
from chipload.program_runner import ProgramProcessor
from chipload.program_sandbox import DockerProgramRunner
from chipload.sandbox import DockerBuilder, WorkerEnvironment
from examples.federation_shops import PROFILES, project, query


@unittest.skipUnless(os.environ.get("CHIPLOAD_WORKER_TESTS") == "1", "requires a built Docker worker")
class ListingWorkerLiveTests(unittest.TestCase):
    def test_exported_listings_build_and_hosted_python_preserves_supplier_business_results(self):
        builder = DockerBuilder(WorkerEnvironment.inspect(Path(".local/worker-image").read_text().strip()))
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "data/shop.db")
            service = ShopService(repository)
            operations = Operations(service)
            owner = Principal(subject="owner", grants=tuple(Grant(shop_id=name, actions=ACTIONS) for name in PROFILES))
            runner = DockerProgramRunner(builder.environment, repository)
            processor = ProgramProcessor(repository, runner, lambda _: owner)
            for profile in PROFILES:
                source = project(profile)
                service.save_source(owner, source, expected_head=None)
                job = service.request_build(owner, profile, source.revision)
                BuildProcessor(repository, builder).run(job.id)
                report = service.get_build(owner, profile, job.id).report
                self.assertEqual(report.status, "succeeded", report.model_dump_json())
                self.assertEqual(len(report.tests), 4)
                self.assertTrue(all(case.passed for case in report.tests))
                service.publish(owner, profile, job.id, expected_generation=0)
                catalog = operations.published.catalog(profile)
                self.assertEqual(catalog["listings"][0]["id"], "service")
                requests = [query(profile, revision=catalog["public_sha256"], count=count, documentation=docs)
                            for count, docs in (("100", "complete"), ("0", "complete"), ("100", "incomplete"))]
                run = operations.programs.submit(owner, RunPython(
                    shop_id=profile, request_key="listing-queries",
                    code="shop = client.shop(inputs['shop_id'])\nresult = [shop.evaluate_listing('service', q)['outcome'] for q in inputs['queries']]",
                    inputs={"shop_id": profile, "queries": [request.model_dump(mode="json") for request in requests]},
                ))
                try:
                    self.assertTrue(processor.run_next())
                    result = operations.programs.read(owner, profile, run.id)
                    self.assertEqual(result.state, "succeeded", str(result))
                    self.assertEqual(result.value, ["supported", "unsupported", "conditional"])
                finally:
                    runner.remove(run.id)
