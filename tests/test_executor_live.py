"""The minimal executor image runs simulators without a network or server dependencies."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from execution_support import ExecutionFixture

from chipload.lifecycle import now
from examples.callable_shop import SIMULATORS

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(
    os.environ.get("CHIPLOAD_ADAPTER_TESTS") == "1", "requires built executor image"
)
class ExecutorLiveTests(unittest.TestCase):
    def test_contained_simulator_uses_only_paired_storage_and_no_network(self):
        with tempfile.TemporaryDirectory(prefix="chipload-executor-") as temporary:
            root = Path(temporary)
            fixture = ExecutionFixture(root / "data/chipload.db")
            fixture.at = now()
            settings, adapters = root / "settings.json", root / "simulators.json"
            for path, value in (
                (
                    settings,
                    {
                        "database": "/data/chipload.db",
                        "bindings": [fixture.owner.model_dump(mode="json")],
                    },
                ),
                (
                    adapters,
                    {
                        "adapters": [
                            {
                                "id": name,
                                "outputs": [fact.model_dump(mode="json") for fact in outputs],
                            }
                            for name, outputs in SIMULATORS.items()
                        ]
                    },
                ),
            ):
                path.touch(mode=0o600)
                path.write_text(json.dumps(value))
            name = "chipload-executor-" + uuid.uuid4().hex[:12]
            command = [
                sys.executable,
                str(ROOT / "scripts/executor"),
                "--settings",
                str(settings),
                "--data",
                str(root / "data"),
                "--simulators",
                str(adapters),
                "--name",
                name,
            ]

            def run(*args):
                completed = subprocess.run(args, capture_output=True, text=True, timeout=60)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                return completed.stdout.strip()

            job = fixture.submit()
            try:
                run(*command, "up")
                for _ in range(100):
                    result = fixture.read(job.id)
                    if result.state == "succeeded":
                        break
                    time.sleep(0.1)
                self.assertEqual(result.state, "succeeded")
                self.assertEqual(result.mode, "simulated")
                container = run(
                    "docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={name}"
                )
                value = json.loads(run("docker", "inspect", container))[0]
                self.assertEqual(value["HostConfig"]["NetworkMode"], "none")
                self.assertEqual(value["HostConfig"]["CapDrop"], ["ALL"])
                self.assertTrue(value["HostConfig"]["ReadonlyRootfs"])
                self.assertEqual(value["HostConfig"]["PidsLimit"], 32)
                self.assertEqual(value["HostConfig"]["Memory"], 512 * 1024**2)
                self.assertFalse(value["NetworkSettings"]["Ports"])
                self.assertEqual(
                    {m["Destination"] for m in value["Mounts"]},
                    {"/data", "/etc/chipload/settings.json", "/etc/chipload/adapters.json"},
                )
                run(
                    "docker",
                    "exec",
                    container,
                    "python",
                    "-I",
                    "-c",
                    "import importlib.util; assert importlib.util.find_spec('fastapi') is None",
                )
                with fixture.repository.connection() as db:
                    self.assertEqual(
                        db.execute("SELECT COUNT(*) FROM simulation_effects").fetchone()[0], 1
                    )
            finally:
                run(*command, "down")
