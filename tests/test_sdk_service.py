"""Repository mount admission and cleanup of both Python execution containers."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chipload.lifecycle import Repository
from chipload.program_sandbox import DockerProgramRunner
from chipload.sandbox import SandboxError
from chipload.sdk_service import data_directory, local_daemon


class SDKServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "data/shop.db"
        Repository(self.database)

    def test_data_mount_requires_only_private_existing_paired_repository_files(self):
        self.assertEqual(data_directory(self.database), self.database.parent)
        settings = self.database.parent / "settings.json"
        settings.write_text("{}")
        settings.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "dedicated directory"):
            data_directory(self.database)
        settings.unlink()
        self.database.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private"):
            data_directory(self.database)
        self.database.chmod(0o600)
        admission = Path(str(self.database) + ".admission")
        admission.rename(self.root / "admission")
        with self.assertRaisesRegex(ValueError, "both existing"):
            data_directory(self.database)
        admission.symlink_to(self.root / "admission")
        with self.assertRaisesRegex(ValueError, "private"):
            data_directory(self.database)

    def test_remote_context_cannot_mount_local_repository_paths(self):
        with (
            patch.dict(
                os.environ, {"DOCKER_HOST": "unix:///local.sock", "DOCKER_CONTEXT": "remote"}
            ),
            patch("chipload.sdk_service.subprocess.check_output", return_value="ssh://remote\n"),
        ):
            with self.assertRaisesRegex(ValueError, "local Unix"):
                local_daemon()

    def test_cleanup_attempts_both_containers_and_does_not_confirm_partial_removal(self):
        runner = object.__new__(DockerProgramRunner)
        identifier = "a" * 32
        with patch("chipload.program_sandbox.subprocess.run") as run:
            run.side_effect = [
                SimpleNamespace(returncode=1, stderr=b"daemon unavailable"),
                SimpleNamespace(returncode=1, stderr=b"daemon unavailable"),
                SimpleNamespace(returncode=0, stderr=b""),
            ]
            with self.assertRaises(SandboxError):
                runner.remove(identifier)
            self.assertEqual(
                [c.args[0][-1] for c in run.call_args_list if c.args[0][1] == "rm"],
                ["chipload-sdk-" + identifier, "chipload-code-" + identifier],
            )
        with patch("chipload.program_sandbox.subprocess.run") as run:
            run.side_effect = [
                subprocess.TimeoutExpired("docker", 15),
                SimpleNamespace(returncode=1, stderr=b"daemon unavailable"),
                SimpleNamespace(returncode=1, stderr=b"No such container"),
            ]
            with self.assertRaises(SandboxError):
                runner.remove(identifier)
            self.assertEqual(run.call_count, 3)

    def test_concurrent_removal_requires_observed_absence(self):
        runner = object.__new__(DockerProgramRunner)
        with patch("chipload.program_sandbox.subprocess.run") as run:
            run.side_effect = [
                SimpleNamespace(returncode=1, stderr=b"removal in progress"),
                SimpleNamespace(returncode=0, stderr=b""),
                SimpleNamespace(returncode=1, stderr=b"No such container"),
                SimpleNamespace(returncode=0, stderr=b""),
            ]
            runner.remove("b" * 32)
            self.assertEqual(run.call_count, 4)

    def test_root_broker_is_rejected_before_container_access(self):
        with patch("chipload.program_sandbox.os.getuid", return_value=0):
            with self.assertRaisesRegex(ValueError, "non-root"):
                DockerProgramRunner(None, None)


if __name__ == "__main__":
    unittest.main()
