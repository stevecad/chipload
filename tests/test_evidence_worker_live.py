"""A shop agent revokes evidence through actual hosted Python and SDK containers."""

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
from examples.alternative_routes import project


@unittest.skipUnless(
    os.environ.get("CHIPLOAD_WORKER_TESTS") == "1", "requires a built Docker worker"
)
class EvidenceWorkerLiveTests(unittest.TestCase):
    def test_agent_reads_revokes_and_queries_through_actual_sdk_service(self):
        builder = DockerBuilder(
            WorkerEnvironment.inspect(Path(".local/worker-image").read_text().strip())
        )
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "data/shop.db")
            service = ShopService(repository)
            source = project("review")
            owner = Principal(
                subject="owner", grants=(Grant(shop_id=source.shop_id, actions=ACTIONS),)
            )
            service.save_source(owner, source, expected_head=None)
            job = service.request_build(owner, source.shop_id, source.revision)
            BuildProcessor(repository, builder).run(job.id)
            report = service.get_build(owner, source.shop_id, job.id).report
            self.assertEqual(report.status, "succeeded", str(report))
            service.publish(owner, source.shop_id, job.id, expected_generation=0)
            operations = Operations(service)
            runner = DockerProgramRunner(builder.environment, repository)
            processor = ProgramProcessor(repository, runner, lambda _: owner)
            request = RunPython(
                shop_id=source.shop_id,
                request_key=uuid.uuid4().hex,
                code="""shop = client.shop(inputs['shop_id'])
before = shop.evaluate(inputs['query'])['outcome']
proofs = shop.evidence()
receipt = shop.revoke_evidence('small-lot-declaration', expected_model_sha256=proofs['revision'], reason='Qualification withdrawn')
read_back = shop.evidence_revocation('small-lot-declaration')
result = {'before': before, 'after': shop.evaluate(inputs['query'])['outcome'], 'same_receipt': receipt == read_back}
""",
                inputs={
                    "shop_id": source.shop_id,
                    "query": source.tests[0].request.model_dump(mode="json"),
                },
            )
            run = operations.programs.submit(owner, request)
            try:
                self.assertTrue(processor.run_next())
                completed = operations.programs.read(owner, source.shop_id, run.id)
                self.assertEqual(completed.state, "succeeded", str(completed))
                self.assertEqual(
                    completed.value,
                    {"before": "supported", "after": "unknown", "same_receipt": True},
                )
            finally:
                runner.remove(run.id)


if __name__ == "__main__":
    unittest.main()
