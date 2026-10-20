"""Build every process/business example from its actual source in disposable containers."""

import os
import unittest
from pathlib import Path

from chipload.sandbox import DockerBuilder, WorkerEnvironment
from examples.process_shops import mixed
from examples.process_shops.export import project
from examples.process_shops.library import BY_ID
from examples.process_shops.shops import define_shop


@unittest.skipUnless(
    os.environ.get("CHIPLOAD_WORKER_TESTS") == "1", "requires a built Docker worker"
)
class ProcessWorkerTests(unittest.TestCase):
    def test_every_process_and_business_bundle_builds_in_the_isolated_worker(self):
        image = (
            os.environ.get("CHIPLOAD_WORKER_IMAGE")
            or Path(".local/worker-image").read_text().strip()
        )
        builder = DockerBuilder(WorkerEnvironment.inspect(image))
        for id in (*BY_ID, "mixed"):
            with self.subTest(process=id):
                source = project(id)
                report = builder.build(source)
                self.assertEqual(report.status, "succeeded", report.model_dump_json())
                expected = mixed.define_shop() if id == "mixed" else define_shop(BY_ID[id])
                self.assertEqual(report.artifact.shop, expected)
                self.assertEqual(len(report.tests), len(source.tests))
                self.assertTrue(all(result.passed for result in report.tests))
