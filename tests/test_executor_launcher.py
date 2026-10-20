"""Operator containment checks before Compose can mount data or credentials."""

import fcntl
import importlib.machinery
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("executor_launcher", str(ROOT / "scripts/executor"))
script = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(script)


class ExecutorLauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_network_requires_internal_bridge_without_api_broker_or_worker(self):
        safe = {"Internal": True, "Driver": "bridge", "Scope": "local", "Containers": {}}
        for change in ({"Internal": False}, {"Driver": "host"}, {"Scope": "swarm"}):
            with (
                self.subTest(change=change),
                patch.object(
                    script.subprocess, "check_output", return_value=json.dumps([{**safe, **change}])
                ),
                self.assertRaises(ValueError),
            ):
                script.private_network("adapter")
        for labels in (
            {"org.chipload.runtime-role": "api"},
            {"org.chipload.runtime-role": "broker"},
            {"org.chipload.worker-protocol": "1"},
        ):
            with (
                self.subTest(labels=labels),
                patch.object(
                    script.subprocess,
                    "check_output",
                    side_effect=[
                        json.dumps([{**safe, "Containers": {"existing": {}}}]),
                        json.dumps(labels),
                    ],
                ),
                self.assertRaises(ValueError),
            ):
                script.private_network("adapter")
        with patch.object(
            script.subprocess,
            "check_output",
            side_effect=[json.dumps([{**safe, "Containers": {"receiver": {}}}]), "{}"],
        ):
            script.private_network("adapter")

    def test_project_collision_is_rejected_even_for_shutdown(self):
        with patch.object(script.subprocess, "check_output", side_effect=["existing\n", "{}"]):
            with self.assertRaisesRegex(ValueError, "different executor"):
                script.ownership("some-project", "expected-owner")

    def test_pending_recovery_blocks_start_but_allows_shutdown(self):
        metadata = self.root / "deployment.json"
        metadata.touch(mode=0o600)
        settings, data = self.root / "runtime.json", self.root / "data"
        for marker in (
            ".restore-incomplete.json",
            ".backup-incomplete.json",
            ".credentials-incomplete.json",
        ):
            path = self.root / marker
            path.touch(mode=0o600)
            with self.subTest(marker=marker):
                with self.assertRaises(ValueError), script.deployment_guard(settings, data, "up"):
                    self.fail("startup admitted unfinished deployment")
                with script.deployment_guard(settings, data, "down"):
                    pass
            path.unlink()
        with (
            self.assertRaises(ValueError),
            script.deployment_guard(settings, self.root / "other", "up"),
        ):
            self.fail("accepted another deployment's data")

    def test_deployment_operation_lock_excludes_concurrent_executor_start(self):
        (self.root / "deployment.json").touch(mode=0o600)
        lock = self.root / ".operation.lock"
        lock.touch(mode=0o600)
        with lock.open("r+") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with (
                self.assertRaisesRegex(ValueError, "another deployment"),
                script.deployment_guard(self.root / "runtime.json", self.root / "data", "up"),
            ):
                self.fail("accepted a concurrent recovery operation")

    def test_missing_database_pair_prevents_compose_or_image_access(self):
        config, adapters, data = (
            self.root / "settings.json",
            self.root / "simulators.json",
            self.root / "data",
        )
        for path, value in (
            (config, '{"database":"/data/chipload.db","bindings":[]}'),
            (adapters, '{"adapters":[]}'),
        ):
            path.touch(mode=0o600)
            path.write_text(value)
        data.mkdir(mode=0o700)
        (data / "chipload.db").touch(mode=0o600)
        args = SimpleNamespace(
            settings=config,
            data=data,
            simulators=adapters,
            adapter=None,
            credentials=None,
            network=None,
            name="fixture",
            action="up",
        )
        with (
            patch.object(script.runtime, "docker_socket"),
            patch.object(script.runtime, "image", return_value="sha256:" + "0" * 64),
            patch.object(script, "ownership"),
            patch.object(script.runtime, "check_images") as images,
            patch.object(script.subprocess, "run") as run,
            self.assertRaises(FileNotFoundError),
        ):
            script.launch(args)
        images.assert_not_called()
        run.assert_not_called()

    def test_simulators_cannot_mount_credentials(self):
        with (
            patch(
                "sys.argv",
                [
                    "executor",
                    "up",
                    "--settings",
                    "settings",
                    "--data",
                    "data",
                    "--simulators",
                    "simulators",
                    "--credentials",
                    "credentials",
                ],
            ),
            patch.object(script.os, "getuid", return_value=1001),
            patch.object(script, "launch") as launch,
            self.assertRaises(SystemExit) as caught,
        ):
            script.main()
        self.assertEqual(caught.exception.code, 2)
        launch.assert_not_called()
