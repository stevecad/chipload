"""Actual asset transport, isolated builds/assessments, publication and rollback."""

import hashlib
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from program_support import wire
from server_support import IdentityFixture

from chipload.access import ACTIONS, Grant, Principal
from chipload.auth import TokenVerifier
from chipload.lifecycle import BuildProcessor, Repository, ShopService
from chipload.operations import Operations
from chipload.program_model import RunPython
from chipload.program_runner import ProgramProcessor
from chipload.program_sandbox import DockerProgramRunner
from chipload.project import MAX_ASSET_BYTES, SourceAsset, SourceProject
from chipload.quotas import QuotaPolicy
from chipload.sandbox import DockerBuilder, WorkerEnvironment
from chipload.server import create_app
from examples import asset_shop


@unittest.skipUnless(os.environ.get("CHIPLOAD_WORKER_TESTS") == "1", "requires rebuilt worker image")
class ProjectAssetLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = WorkerEnvironment.inspect(Path(".local/worker-image").read_text().strip())

    def test_agent_authors_binary_geometry_and_rolls_back_its_assessments(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = IdentityFixture(Path(directory) / "data")
            self.addCleanup(identity.client.close)
            owner = Principal(subject="owner", grants=(Grant(shop_id=asset_shop.SHOP, actions=ACTIONS),))
            outsider = Principal(subject="outsider", grants=(Grant(shop_id="another-shop", actions=ACTIONS),))
            settings = identity.settings.model_copy(update={"bindings": (owner, outsider)})
            repository = Repository(settings.database)
            # Admit the complete lifecycle batch without relying on refill time.
            quota = repository.read_quotas()
            for scope in ("peer", "identity", "mutation_identity", "mutation_shop"):
                quota["policy"]["requests"][scope]["burst"] = 100
            repository.set_quotas(
                QuotaPolicy.model_validate(quota["policy"]),
                expected_generation=quota["generation"],
            )
            service = ShopService(repository)
            operations = Operations(service)
            builder = BuildProcessor(repository, DockerBuilder(self.environment))
            processor = ProgramProcessor(repository, DockerProgramRunner(self.environment, repository),
                                         {"owner": owner, "outsider": outsider}.get)
            app = create_app(settings, operations=operations, verifier=TokenVerifier(settings, client=identity.client))
            headers = {"Authorization": "Bearer " + identity.token()}
            base = f"/v1/shops/{asset_shop.SHOP}"
            with TestClient(app, base_url="http://localhost:8088") as http:
                def code(program, inputs=None):
                    response = wire(http, "run_python", {
                        "shop_id": asset_shop.SHOP, "request_key": uuid.uuid4().hex, "code": program,
                        "inputs": inputs or {},
                    }, headers).json()["result"]
                    self.assertFalse(response.get("isError"), response)
                    self.assertTrue(processor.run_next())
                    result = operations.programs.read(owner, asset_shop.SHOP, response["structuredContent"]["id"])
                    self.assertEqual(result.state, "succeeded", result)
                    return result.value

                def build(source):
                    submitted = code("result=client.shop(inputs['shop_id']).build(inputs['revision'],request_key=inputs['key'])",
                                     {"shop_id": source.shop_id, "revision": source.revision, "key": uuid.uuid4().hex})
                    builder.run(submitted["id"])
                    built = service.get_build(owner, source.shop_id, submitted["id"])
                    self.assertEqual(built.state, "succeeded", built.report)
                    self.assertEqual(len(built.report.tests), 8)
                    self.assertTrue(all(case.passed for case in built.report.tests))
                    return built

                def assess(query):
                    revision = http.get(base + "/catalog").json()["public_sha256"]
                    request = {"shop_id": asset_shop.SHOP, "request_key": uuid.uuid4().hex,
                               "query": query.model_dump(mode="json"), "expected_revision": revision}
                    queued = http.post(base + "/assessments", json=request, headers=headers)
                    self.assertEqual(queued.status_code, 202, queued.text)
                    self.assertTrue(processor.run_next())
                    rest = http.get(base + "/assessments/" + queued.json()["id"], headers=headers).json()
                    agent = code("result=client.shop(inputs['shop_id']).assessment(inputs['id'])",
                                 {"shop_id": asset_shop.SHOP, "id": queued.json()["id"]})
                    self.assertEqual(rest, agent)
                    self.assertEqual(rest["state"], "succeeded", rest)
                    return rest

                source = asset_shop.project()
                saved = code("result=client.shop(inputs['shop_id']).save(inputs['project'],expected_head=None)",
                             {"shop_id": source.shop_id, "project": source.model_dump(mode="json")})
                self.assertEqual(saved["revision"], source.revision)
                restored = SourceProject.model_validate(http.get(base + "/source", headers=headers).json()["project"])
                self.assertEqual(restored.assets[0].read_bytes(), asset_shop.mesh())
                for caller, status in (({}, 401), ({"Authorization": "Bearer " + identity.token("outsider")}, 403)):
                    self.assertEqual(http.get(base + "/source", headers=caller).status_code, status)
                first = build(source)
                code("result=client.shop(inputs['shop_id']).publish(inputs['id'],expected_generation=0)",
                     {"shop_id": source.shop_id, "id": first.id})
                public = http.get(base + "/catalog").text
                self.assertNotIn(source.assets[0].content_base64, public)
                self.assertNotIn(asset_shop.PRIVATE_MARKER.decode(), public)
                self.assertNotIn("fixture.stl", public)
                self.assertEqual(assess(asset_shop.request())["evaluation"]["outcome"], "supported")
                larger_query = asset_shop.request(width=asset_shop.quantity("130", "mm"))
                self.assertEqual(assess(larger_query)["evaluation"]["outcome"], "unsupported")

                larger = asset_shop.project(width=140)
                changed = http.put(base + "/source", headers=headers, json={
                    "project": larger.model_dump(mode="json"), "expected_head": source.revision})
                self.assertEqual(changed.status_code, 200, changed.text)
                second = build(larger)
                self.assertNotEqual(first.report.artifact.model_sha256, second.report.artifact.model_sha256)
                service.publish(owner, source.shop_id, second.id, expected_generation=1)
                supported = assess(larger_query)
                self.assertEqual(supported["evaluation"]["outcome"], "supported")
                code("result=client.shop(inputs['shop_id']).rollback(inputs['id'],expected_generation=2)",
                     {"shop_id": source.shop_id, "id": first.id})
                self.assertEqual(assess(larger_query)["evaluation"]["outcome"], "unsupported")
                self.assertEqual(service.get_source(owner, source.shop_id).revision, larger.revision)
                stale = http.get(base + "/assessments/" + supported["id"], headers=headers).json()
                self.assertEqual(stale["state"], "superseded")

                broken = larger.model_copy(update={"assets": (SourceAsset.from_bytes(path="data/fixture.stl", content=b"broken"),)})
                service.save_source(owner, broken, expected_head=larger.revision)
                job = service.request_build(owner, source.shop_id, broken.revision)
                builder.run(job.id)
                failed = service.get_build(owner, source.shop_id, job.id)
                self.assertEqual(failed.state, "failed")
                self.assertEqual(failed.report.diagnostics[0].code, "source-error")
                self.assertEqual(service.get_publication(owner, source.shop_id).build_id, first.id)

    def test_largest_asset_is_opaque_and_builds_reproducibly(self):
        source = asset_shop.project()
        payload = b"\0" * MAX_ASSET_BYTES
        source = source.model_copy(update={
            "assets": (*source.assets, SourceAsset.from_bytes(path="data/opaque.bin", content=payload),
                       SourceAsset.from_bytes(path="unused.py", content=b"raise RuntimeError('never import an asset automatically')")),
            "files": (source.files[0].model_copy(update={"content": source.files[0].content +
                      "\nassert (Path(__file__).parent / 'data/opaque.bin').read_bytes() == b'\\0' * 1048576\n"}), source.files[1]),
        })
        builder = DockerBuilder(self.environment)
        first, second = builder.build(source), builder.build(source)
        self.assertEqual(first.status, "succeeded", first)
        self.assertEqual(second.status, "succeeded", second)
        self.assertEqual(first.artifact.build_sha256, second.artifact.build_sha256)
        self.assertEqual(first.tests, second.tests)

    def test_agent_creates_and_inspects_maximum_asset_through_contained_sdk(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "data" / "shop.db")
            operations = Operations(ShopService(repository))
            owner = Principal(subject="owner", grants=(Grant(shop_id=asset_shop.SHOP, actions=ACTIONS),))
            source = asset_shop.project()
            request = RunPython(shop_id=source.shop_id, request_key="large-asset", inputs={"project": source.model_dump(mode="json")}, code="""
import hashlib
from chipload.project import SourceAsset, SourceProject
source = SourceProject.model_validate(inputs['project'])
asset = SourceAsset.from_bytes(path='data/opaque.bin', content=b'\\0' * 1048576)
source = source.model_copy(update={'assets': (*source.assets, asset)})
shop = client.shop(source.shop_id)
saved = shop.save(source, expected_head=None)
read = SourceProject.model_validate(shop.source()['project'])
value = next(asset for asset in read.assets if asset.path == 'data/opaque.bin')
result = {'revision': saved['revision'], 'sha256': hashlib.sha256(value.read_bytes()).hexdigest()}
""")
            queued = operations.programs.submit(owner, request)
            processor = ProgramProcessor(repository, DockerProgramRunner(self.environment, repository), lambda _: owner)
            self.assertTrue(processor.run_next())
            result = operations.programs.read(owner, source.shop_id, queued.id)
            self.assertEqual(result.state, "succeeded", result)
            self.assertEqual(result.value["sha256"], hashlib.sha256(b"\0" * MAX_ASSET_BYTES).hexdigest())
            stored = operations.service.get_source(owner, source.shop_id)
            self.assertEqual(result.value["revision"], stored.revision)
            self.assertEqual(len(stored.project.assets), 2)


if __name__ == "__main__":
    unittest.main()
