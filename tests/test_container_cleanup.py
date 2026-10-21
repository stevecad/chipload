"""Ambiguous Docker responses cannot release capacity without confirmed removal."""

import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from chipload.artifact import sdk_fingerprint
from chipload.containers import CLEANUP_SECONDS, SandboxError, remove_containers
from chipload.project import SourceFile, SourceProject
from chipload.sandbox import DockerBuilder, WorkerEnvironment

SDK = "chipload-sdk-" + "a" * 32
CODE = "chipload-code-" + "a" * 32


def response(code=0, error=b""):
    return SimpleNamespace(returncode=code, stderr=error)


class ContainerCleanupTests(unittest.TestCase):
    def test_lost_removal_reply_is_resolved_only_by_observed_absence(self):
        with patch("chipload.containers.subprocess.run") as run:
            run.side_effect = [
                subprocess.TimeoutExpired("docker rm", 5),
                response(1, b"Error: No such container: " + SDK.encode()),
                response(),
            ]
            remove_containers(SDK, CODE)
        self.assertEqual([c.args[0][1] for c in run.call_args_list], ["rm", "container", "rm"])

    def test_competing_removal_may_take_longer_than_two_seconds(self):
        at = 0.0

        def sleep(seconds):
            nonlocal at
            at += seconds

        def docker(command, **kwargs):
            if command[1] == "rm":
                return response(1, b"removal in progress")
            return response() if at < 3 else response(1, b"No such object")

        with (
            patch("chipload.containers.time.monotonic", side_effect=lambda: at),
            patch("chipload.containers.time.sleep", side_effect=sleep),
            patch("chipload.containers.subprocess.run", side_effect=docker),
        ):
            remove_containers(SDK, CODE)
        self.assertGreaterEqual(at, 3)
        self.assertLess(at, CLEANUP_SECONDS)

    def test_stuck_removal_retains_failure_and_still_attempts_the_other_container(self):
        at = 0.0
        attempted = []

        def sleep(seconds):
            nonlocal at
            at += seconds

        def docker(command, *, timeout, **kwargs):
            nonlocal at
            self.assertGreater(timeout, 0)
            if command[1] == "rm":
                attempted.append(command[-1])
                return response(1, b"removal in progress")
            at += timeout
            raise subprocess.TimeoutExpired(command, timeout)

        with (
            patch("chipload.containers.time.monotonic", side_effect=lambda: at),
            patch("chipload.containers.time.sleep", side_effect=sleep),
            patch("chipload.containers.subprocess.run", side_effect=docker),
        ):
            with self.assertRaises(SandboxError):
                remove_containers(SDK, CODE)
        self.assertEqual(attempted, [SDK, CODE])
        self.assertLessEqual(at, CLEANUP_SECONDS)

    def test_unavailable_inspection_is_not_absence(self):
        with patch("chipload.containers.subprocess.run") as run:
            run.side_effect = [
                response(1, b"removal in progress"),
                response(1, b"permission denied"),
                response(),
            ]
            with self.assertRaises(SandboxError):
                remove_containers(SDK, CODE)
        self.assertEqual(run.call_args.args[0][-1], CODE)

    def test_names_cannot_target_other_containers(self):
        with patch("chipload.containers.subprocess.run") as run:
            for names in ((), ("other-service",), (SDK, CODE, SDK), (SDK + "\n",)):
                with self.subTest(names=names), self.assertRaises(ValueError):
                    remove_containers(*names)
            run.assert_not_called()

    def test_builder_does_not_return_a_terminal_report_after_failed_cleanup(self):
        environment = WorkerEnvironment("sha256:" + "a" * 64, sdk_fingerprint(), "b" * 64, "3.12.15")
        builder = DockerBuilder(environment)
        source = SourceProject(
            shop_id="example", entrypoint="shop:define",
            files=(SourceFile(path="shop.py", content="def define(): pass"),),
        )
        with (
            patch("chipload.sandbox.subprocess.run", return_value=response()),
            patch("chipload.sandbox.exchange", side_effect=SandboxError("worker interrupted")),
            patch.object(builder, "remove", side_effect=SandboxError("cleanup unavailable")),
        ):
            with self.assertRaisesRegex(SandboxError, "cleanup unavailable"):
                builder.build(source)


if __name__ == "__main__":
    unittest.main()
