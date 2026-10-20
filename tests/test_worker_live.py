"""Opt-in checks against actual containers, including hostile shop source."""

import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from chipload.access import ACTIONS, Grant, Principal
from chipload.lifecycle import BuildProcessor, Conflict, Repository, ShopService
from chipload.project import SourceFile, SourceProject
from chipload.sandbox import DockerBuilder, WorkerEnvironment
from examples.export_project import project


@unittest.skipUnless(
    os.environ.get("CHIPLOAD_WORKER_TESTS") == "1", "requires a built Docker worker"
)
class WorkerLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        image = (
            os.environ.get("CHIPLOAD_WORKER_IMAGE")
            or Path(".local/worker-image").read_text().strip()
        )
        cls.environment = WorkerEnvironment.inspect(image)

    def source(self, code, *, files=()):
        return SourceProject(
            shop_id="example",
            entrypoint="shop:define_shop",
            files=(SourceFile(path="shop.py", content=code), *files),
        )

    def build(self, source, seconds=10):
        job = uuid.uuid4().hex
        report = DockerBuilder(self.environment, seconds=seconds).build(source, job_id=job)
        found = subprocess.run(
            ["docker", "container", "inspect", f"chipload-build-{job}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.assertNotEqual(found.returncode, 0, "worker was left behind")
        return report

    def test_multi_module_project_with_data_files(self):
        source = self.source(
            "from components import define_shop\n",
            files=(
                SourceFile(
                    path="components.py",
                    content=(
                        "import json\nfrom pathlib import Path\nfrom chipload import Shop\n"
                        "def define_shop():\n"
                        "    data = json.loads(Path(__file__).with_name('data.json').read_text())\n"
                        "    return Shop(id='example', name=data['name'])\n"
                    ),
                ),
                SourceFile(path="data.json", content='{"name":"Composed shop"}'),
            ),
        )
        one = self.build(source)
        two = self.build(source)
        self.assertEqual(one.status, "succeeded", one.model_dump_json())
        self.assertEqual(one.artifact.shop.name, "Composed shop")
        self.assertEqual(one.artifact.build_sha256, two.artifact.build_sha256)
        self.assertIn(self.environment.image, one.artifact.identity.runtime)

    def test_runtime_cannot_access_host_files_environment_network_or_write_root(self):
        with tempfile.NamedTemporaryFile(prefix="chipload-host-canary-") as file:
            code = f"""
import os
import socket
from pathlib import Path
from chipload import Shop
def define_shop():
    assert os.getuid() == 65532
    assert not Path({file.name!r}).exists()
    assert not Path('/var/run/docker.sock').exists()
    assert os.environ.get('CHIPLOAD_HOST_TEST_SECRET') is None
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())
    assert int(status['CapEff'].strip(), 16) == 0
    assert status['NoNewPrivs'].strip() == '1'
    assert status['Seccomp'].strip() == '2'
    try:
        Path('/usr/local/lib/python3.12/site-packages/chipload/worker.py').write_text('overwritten')
    except OSError:
        pass
    else:
        raise AssertionError('SDK filesystem was writable')
    try:
        socket.create_connection(('192.0.2.1', 9), timeout=0.1)
    except OSError:
        pass
    else:
        raise AssertionError('outbound network was available')
    return Shop(id='example', name='Isolation checked')
"""
            with patch.dict(os.environ, {"CHIPLOAD_HOST_TEST_SECRET": "host-only-test-canary"}):
                report = self.build(self.source(code))
        self.assertEqual(report.status, "succeeded", report.model_dump_json())

    def test_container_configuration_has_limits_and_no_host_mounts(self):
        job = uuid.uuid4().hex
        builder = DockerBuilder(self.environment)
        name = f"chipload-build-{job}"
        subprocess.run(builder.create_command(name), check=True, capture_output=True)
        try:
            info = json.loads(subprocess.check_output(["docker", "inspect", name]))[0]
            host = info["HostConfig"]
            self.assertEqual(info["Config"]["User"], "65532:65532")
            self.assertEqual(host["NetworkMode"], "none")
            self.assertTrue(host["ReadonlyRootfs"])
            self.assertEqual(host["CapDrop"], ["ALL"])
            self.assertEqual(host["Memory"], 256 * 1024 * 1024)
            self.assertEqual(host["MemorySwap"], host["Memory"])
            self.assertEqual(host["PidsLimit"], 64)
            self.assertEqual(host["NanoCpus"], 1_000_000_000)
            self.assertEqual(info["Mounts"], [])
            self.assertIn("noexec", host["Tmpfs"]["/tmp"])
        finally:
            builder.remove(job)

    def test_infinite_loop_is_terminated_and_removed(self):
        report = self.build(self.source("while True: pass\n"), seconds=1)
        self.assertEqual(report.status, "failed")
        self.assertIn("wall-time", report.diagnostics[0].message)

    def test_output_flood_is_bounded_and_removed(self):
        report = self.build(self.source("import os\nwhile True: os.write(1, b'x' * 65536)\n"))
        self.assertEqual(report.status, "failed")
        self.assertIn("output limit", report.diagnostics[0].message)

    def test_excessive_memory_is_terminated_and_removed(self):
        report = self.build(self.source("allocation = bytearray(400 * 1024 * 1024)\n"))
        self.assertEqual(report.status, "failed")
        self.assertTrue(
            any("137" in d.message or "MemoryError" in d.message for d in report.diagnostics),
            report.model_dump_json(),
        )

    def test_source_error_has_repair_location(self):
        report = self.build(self.source("def define_shop(:\n    pass\n"))
        self.assertEqual(report.status, "failed")
        self.assertEqual(report.diagnostics[0].code, "source-error")
        self.assertEqual(report.diagnostics[0].path, ("shop.py", "1"))

    def test_forged_output_is_revalidated_outside_source_process(self):
        # Bypass the SDK inside the worker and send a well-shaped but invalid graph.
        from chipload import Capability, Process, Shop

        invalid = Shop(
            id="example",
            name="Forged",
            processes=(Process(id="present", name="Present"),),
            capabilities=(Capability(id="offer", name="Offer", process="missing"),),
        )
        payload = json.dumps({"shop": invalid.model_dump(mode="json")}).encode()
        report = self.build(self.source(f"import os\nos.write(1, {payload!r})\nos._exit(0)\n"))
        self.assertEqual(report.status, "failed")
        self.assertTrue(any(d.code == "missing-reference" for d in report.diagnostics))

    def test_worker_cannot_return_another_shops_model(self):
        report = self.build(
            self.source(
                "from chipload import Shop\n"
                "def define_shop(): return Shop(id='another', name='Another shop')\n"
            )
        )
        self.assertEqual(report.status, "failed")
        self.assertIn("shop ID", report.diagnostics[0].message)

    def test_forged_data_cannot_overrun_parser_or_model_budgets(self):
        from chipload import Capability, Process, Range, Shop, Step, Workflow, quantity

        cap = Capability(
            id="offer", name="Offer", process="process",
            requirements=tuple(
                Range(fact=f"f-{i}", minimum=quantity("0", "mm")) for i in range(64)
            ),
        )
        costly = Shop(
            id="example", name="Repeated work",
            processes=(Process(id="process", name="Process"),),
            capabilities=(cap,),
            workflows=(Workflow(
                id="route", name="Route",
                steps=tuple(Step(id=f"s-{i}", capability=cap.id) for i in range(7)),
            ),),
        )
        attacks = (
            ("b'[' * 33 + b'0' + b']' * 33", "nesting"),
            ("b'[' + b'0,' * 100_000 + b'0]'", "token"),
            ("json.dumps({'shop': {'id': 'example', 'name': 'Forged', "
             "'resources': [None] * 1001}}).encode()", "1000"),
            (repr(json.dumps({"shop": costly.model_dump(mode="json")}).encode()), "work score"),
        )
        for expression, expected in attacks:
            code = (
                "import os, json\n"
                f"payload = {expression}\n"
                "while payload:\n"
                "    sent = os.write(1, payload)\n"
                "    payload = payload[sent:]\n"
                "os._exit(0)\n"
            )
            with self.subTest(expected=expected):
                report = self.build(self.source(code))
                self.assertEqual(report.status, "failed")
                self.assertIsNone(report.artifact)
                self.assertTrue(any(expected in d.message for d in report.diagnostics), report)
        healthy = self.build(self.source(
            "from chipload import Shop\n"
            "def define_shop(): return Shop(id='example', name='Still working')\n"
        ))
        self.assertEqual(healthy.status, "succeeded", healthy)

    def test_saved_edit_build_publish_failure_update_and_rollback(self):
        source = project()
        with tempfile.TemporaryDirectory() as directory:
            service = ShopService(Repository(Path(directory) / "shop.db"))
            processor = BuildProcessor(service.repository, DockerBuilder(self.environment))
            owner = Principal(
                subject="live-test", grants=(Grant(shop_id=source.shop_id, actions=ACTIONS),)
            )

            def build(candidate, expected=None):
                revision = service.save_source(owner, candidate, expected_head=expected)
                record = service.request_build(owner, source.shop_id, revision.revision)
                processor.run(record.id)
                return service.get_build(owner, source.shop_id, record.id)

            first = build(source)
            self.assertEqual(first.state, "succeeded", first.model_dump_json())
            self.assertEqual(len(first.report.tests), 9)
            service.publish(owner, source.shop_id, first.id, expected_generation=0)
            original = service.catalog(source.shop_id)
            broken = source.model_copy(
                update={"files": (SourceFile(path="thermal.py", content="invalid syntax !"),)}
            )
            failed = build(broken, source.revision)
            self.assertEqual(failed.state, "failed")
            with self.assertRaises(Conflict):
                service.publish(owner, source.shop_id, failed.id, expected_generation=1)
            self.assertEqual(service.catalog(source.shop_id), original)
            revised = source.model_copy(
                update={
                    "files": (
                        source.files[0].model_copy(
                            update={
                                "content": source.files[0].content.replace(
                                    "Thermal Processing Example", "Revised shop"
                                )
                            }
                        ),
                    )
                }
            )
            second = build(revised, broken.revision)
            self.assertEqual(second.state, "succeeded", second.model_dump_json())
            service.publish(owner, source.shop_id, second.id, expected_generation=1)
            self.assertEqual(service.catalog(source.shop_id)["shop"]["name"], "Revised shop")
            service.rollback(owner, source.shop_id, first.id, expected_generation=2)
            self.assertEqual(service.catalog(source.shop_id), original)


if __name__ == "__main__":
    unittest.main()
