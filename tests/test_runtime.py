"""Operator startup rejects unsafe storage, stale images, and remote daemons."""

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("runtime_script", str(ROOT / "scripts/runtime"))
script = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(script)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_storage_requires_private_owned_regular_files_and_directories(self):
        config = self.root / "config.json"
        config.write_text("{}")
        config.chmod(0o600)
        script.private(config)
        script.private(self.root, directory=True)
        config.chmod(0o644)
        with self.assertRaises(ValueError):
            script.private(config)
        config.chmod(0o600)
        alias = self.root / "alias.json"
        alias.symlink_to(config)
        with self.assertRaises(ValueError):
            script.private(alias)
        with self.assertRaises(ValueError):
            script.private(config, directory=True)
        with patch.object(script.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(ValueError):
                script.private(config)

    def test_remote_context_wins_over_host_variable_and_is_rejected_before_access(self):
        with (
            patch.dict(
                os.environ,
                {"DOCKER_CONTEXT": "remote", "DOCKER_HOST": "unix:///var/run/docker.sock"},
            ),
            patch.object(script.os, "getuid", return_value=1001),
            patch.object(script.subprocess, "check_output", return_value="ssh://remote-host\n"),
            patch.object(script.subprocess, "run") as run,
            patch("sys.argv", ["runtime", "up"]),
        ):
            with self.assertRaises(SystemExit) as raised:
                script.main()
            self.assertEqual(raised.exception.code, 1)
            run.assert_not_called()

    def test_data_mount_refuses_unrelated_private_files(self):
        script.check_data(self.root)
        database = self.root / "chipload.db"
        database.write_bytes(b"fixture")
        database.chmod(0o600)
        script.check_data(self.root)
        (self.root / "credentials.json").write_text("private fixture")
        with self.assertRaisesRegex(ValueError, "dedicated data directory"):
            script.check_data(self.root)

    def test_root_launcher_is_rejected(self):
        with (
            patch.object(script.os, "getuid", return_value=0),
            patch("sys.argv", ["runtime", "up"]),
        ):
            with self.assertRaises(SystemExit) as raised:
                script.main()
            self.assertEqual(raised.exception.code, 2)

    def test_image_ids_must_be_immutable(self):
        (self.root / ".local").mkdir()
        (self.root / ".local/api-image").write_text("chipload:latest\n")
        with patch.object(script, "ROOT", self.root):
            with self.assertRaises(ValueError):
                script.image("api")

    def test_all_roles_sdk_and_dependencies_are_checked_before_startup(self):
        files = sorted((ROOT / "src/chipload").glob("*.py"))
        sdk = hashlib.sha256(
            b"".join(p.name.encode() + b"\0" + p.read_bytes() + b"\0" for p in files)
        ).hexdigest()
        common = {
            "org.chipload.sdk-sha256": sdk,
            "org.chipload.dependencies-sha256": hashlib.sha256(
                (ROOT / "uv.lock").read_bytes()
            ).hexdigest(),
            "org.chipload.recipe-sha256": hashlib.sha256(
                (ROOT / "deploy/runtime/Dockerfile").read_bytes()
            ).hexdigest(),
        }
        labels = {
            role: {
                **common,
                "org.chipload.runtime-role": role,
                "org.chipload.runtime-protocol": "1",
            }
            for role in ("api", "broker")
        }
        labels["worker"] = {
            **common,
            "org.chipload.worker-protocol": "1",
            "org.chipload.recipe-sha256": hashlib.sha256(
                (ROOT / "deploy/worker/Dockerfile").read_bytes()
            ).hexdigest(),
        }

        def inspect(command, **kwargs):
            return SimpleNamespace(stdout=json.dumps(labels[command[3]]))

        with patch.object(script.subprocess, "run", side_effect=inspect):
            script.check_images({role: role for role in labels})
            with patch.dict(labels["worker"], {
                "org.chipload.core-dependencies-sha256": common["org.chipload.dependencies-sha256"],
                "org.chipload.dependencies-sha256": "c" * 64,
            }):
                script.check_images({role: role for role in labels})
                with patch.dict(labels["worker"], {"org.chipload.core-dependencies-sha256": "d" * 64}):
                    with self.assertRaises(ValueError):
                        script.check_images({role: role for role in labels})
            for role, key, invalid in (
                ("api", "org.chipload.runtime-role", "broker"),
                ("broker", "org.chipload.sdk-sha256", "old-sdk"),
                ("worker", "org.chipload.dependencies-sha256", "old-lock"),
                ("worker", "org.chipload.worker-protocol", "0"),
                ("api", "org.chipload.recipe-sha256", "old-recipe"),
                ("worker", "org.chipload.recipe-sha256", "old-recipe"),
            ):
                with self.subTest(role=role, key=key), patch.dict(labels[role], {key: invalid}):
                    with self.assertRaises(ValueError):
                        script.check_images({name: name for name in labels})


if __name__ == "__main__":
    unittest.main()
