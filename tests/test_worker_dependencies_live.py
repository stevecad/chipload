"""Actual CadQuery builds and dependency startup hooks under container isolation."""

import json
import os
import subprocess
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from server_support import IdentityFixture

from chipload.program_model import RunPython
from chipload.program_runner import ProgramProcessor
from chipload.program_sandbox import DockerProgramRunner
from chipload.project import SourceFile, SourceProject
from chipload.sandbox import DockerBuilder, WorkerEnvironment
from examples.cadquery_shop import project


@unittest.skipUnless(
    os.environ.get("CHIPLOAD_DEPENDENCY_TESTS") == "1", "requires built core and CadQuery workers"
)
class WorkerDependencyLiveTests(unittest.TestCase):
    def test_wrong_wheel_hash_cannot_replace_the_selected_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock, output = root / "requirements.txt", root / "image"
            lock.write_text("attrs==26.1.0 --hash=sha256:" + "0" * 64 + "\n")
            original = Path(".local/core-worker-image").read_bytes()
            output.write_bytes(original)
            failed = subprocess.run(
                ["./scripts/build-worker", "--requirements", str(lock), "--output", str(output)],
                capture_output=True,
                timeout=90,
            )
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn(b"DO NOT MATCH THE HASHES", failed.stderr)
            self.assertEqual(output.read_bytes(), original)

    def test_cadquery_build_is_reproducible_and_core_service_cannot_import_it(self):
        env = WorkerEnvironment.inspect(Path(".local/cadquery-worker-image").read_text().strip())
        builder = DockerBuilder(env)
        source = project(env.dependencies_sha256)
        first, second = builder.build(source), builder.build(source)
        self.assertEqual(first.status, "succeeded", first.model_dump_json())
        self.assertEqual(second.status, "succeeded", second.model_dump_json())
        self.assertEqual(first.artifact.build_sha256, second.artifact.build_sha256)
        self.assertEqual(len(first.tests), 6)
        self.assertTrue(all(case.passed for case in first.tests), first.tests)
        command = builder.create_command("chipload-build-" + uuid.uuid4().hex)
        name = command[command.index("--name") + 1]
        command[command.index("--entrypoint") + 1] = "/usr/local/bin/python"
        command[-2:] = [
            "-c",
            "import importlib.util; assert importlib.util.find_spec('cadquery') is None",
        ]
        try:
            subprocess.run(command, check=True, capture_output=True)
            subprocess.run(["docker", "start", "--attach", name], check=True, capture_output=True)
            state = json.loads(subprocess.check_output(["docker", "inspect", name]))[0]["State"]
            self.assertEqual(state["ExitCode"], 0)
        finally:
            builder.remove(name.removeprefix("chipload-build-"))

        with tempfile.TemporaryDirectory() as directory:
            identity = IdentityFixture(Path(directory) / "data")
            self.addCleanup(identity.client.close)
            operations = identity.operations()
            owner = identity.settings.bindings[0]
            runner = DockerProgramRunner(env, operations.service.repository)
            processor = ProgramProcessor(
                operations.service.repository, runner, lambda _: owner, stopping=threading.Event()
            )
            request = RunPython(
                shop_id="thermal-example",
                request_key=uuid.uuid4().hex,
                code="import cadquery as cq\nvolume = cq.Workplane('XY').box(1,2,3).val().Volume()\n"
                "source = client.shop('thermal-example').source()\n"
                "result = {'volume': round(volume, 6), 'has_source': bool(source)}",
            )
            job = operations.programs.submit(owner, request)
            try:
                self.assertTrue(processor.run_next())
                result = operations.programs.read(owner, job.shop_id, job.id)
                self.assertEqual(result.state, "succeeded", str(result))
                self.assertEqual(result.value, {"volume": 6.0, "has_source": True})
            finally:
                runner.remove(job.id)

    def test_startup_hook_runs_only_in_untrusted_build_and_code_containers(self):
        base = Path(".local/core-worker-image").read_text().strip()
        tag = "chipload-dependency-test-" + uuid.uuid4().hex
        subprocess.run(["docker", "tag", base, tag], check=True, capture_output=True)
        self.addCleanup(subprocess.run, ["docker", "image", "rm", tag], capture_output=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Dockerfile").write_text(
                f"FROM {tag}\nCOPY hook/ /opt/chipload-shop/lib/python3.12/site-packages/\n"
            )
            hook = root / "hook"
            hook.mkdir()
            (hook / "canary.pth").write_text("import shop_hook\n")
            (hook / "shop_hook.py").write_text(
                "import os\nfrom pathlib import Path\n"
                "if Path('/data/platform.db').exists(): os._exit(97)\n"
                "assert not Path('/var/run/docker.sock').exists()\n"
                "assert os.getuid() == 65532\n"
                "os.environ['CHIPLOAD_SHOP_HOOK'] = 'ran'\n"
            )
            subprocess.run(
                ["docker", "build", "--iidfile", str(root / "id"), str(root)],
                check=True,
                capture_output=True,
                timeout=60,
            )
            image = (root / "id").read_text().strip()
            self.addCleanup(subprocess.run, ["docker", "image", "rm", image], capture_output=True)
            env = WorkerEnvironment.inspect(image)
            source = SourceProject(
                shop_id="example",
                entrypoint="shop:define",
                files=(
                    SourceFile(
                        path="shop.py",
                        content="import os\nfrom chipload import Shop\ndef define():\n"
                        "    assert os.environ['CHIPLOAD_SHOP_HOOK'] == 'ran'\n"
                        "    return Shop(id='example', name='Hook was contained')\n",
                    ),
                ),
            )
            report = DockerBuilder(env).build(source)
            self.assertEqual(report.status, "succeeded", report.model_dump_json())
            identity = IdentityFixture(root / "data")
            self.addCleanup(identity.client.close)
            operations = identity.operations()
            owner = identity.settings.bindings[0]
            runner = DockerProgramRunner(env, operations.service.repository)
            processor = ProgramProcessor(
                operations.service.repository, runner, lambda _: owner, stopping=threading.Event()
            )
            request = RunPython(
                shop_id="thermal-example",
                request_key=uuid.uuid4().hex,
                code="import os\nassert os.environ['CHIPLOAD_SHOP_HOOK'] == 'ran'\n"
                "result = client.shop('thermal-example').source()",
            )
            job = operations.programs.submit(owner, request)
            self.addCleanup(runner.remove, job.id)
            self.assertTrue(processor.run_next())
            result = operations.programs.read(owner, job.shop_id, job.id)
            self.assertEqual(result.state, "succeeded", str(result))
            self.assertTrue(result.calls)
