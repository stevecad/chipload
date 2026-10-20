"""An abusive shop's actual worker cannot block another shop's queued build."""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from chipload.access import ACTIONS, Grant, Principal
from chipload.lifecycle import Repository, ShopService
from chipload.project import SourceFile, SourceProject
from chipload.sandbox import DockerBuilder, WorkerEnvironment
from examples.export_project import project


@unittest.skipUnless(
    os.environ.get("CHIPLOAD_WORKER_TESTS") == "1", "requires a built Docker worker"
)
class QuotaWorkerLiveTests(unittest.TestCase):
    def test_other_shop_builds_while_abusive_source_holds_one_slot(self):
        image = (
            os.environ.get("CHIPLOAD_WORKER_IMAGE")
            or Path(".local/worker-image").read_text().strip()
        )
        environment = WorkerEnvironment.inspect(image)
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "queue.db")
            service = ShopService(repository)
            bad = SourceProject(
                shop_id="abusive",
                entrypoint="shop:define_shop",
                files=(SourceFile(path="shop.py", content="while True: pass\n"),),
            )
            good = project()
            bad_owner = Principal(
                subject="bad-owner", grants=(Grant(shop_id=bad.shop_id, actions=ACTIONS),)
            )
            good_owner = Principal(
                subject="good-owner", grants=(Grant(shop_id=good.shop_id, actions=ACTIONS),)
            )
            for who, source in ((bad_owner, bad), (good_owner, good)):
                service.save_source(who, source, expected_head=None)
            first = service.request_build(bad_owner, bad.shop_id, bad.revision)
            second = service.request_build(bad_owner, bad.shop_id, bad.revision)
            healthy = service.request_build(good_owner, good.shop_id, good.revision)
            command = [
                sys.executable,
                "-m",
                "chipload.broker",
                "--database",
                str(repository.path),
                "--image",
                image,
                "--once",
            ]
            abusive_broker = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
            )
            children = [abusive_broker]
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if service.get_build(bad_owner, bad.shop_id, first.id).state == "running":
                        container = subprocess.run(
                            [
                                "docker",
                                "inspect",
                                "--format",
                                "{{.State.Running}}",
                                f"chipload-build-{first.id}",
                            ],
                            capture_output=True,
                            text=True,
                            timeout=5,
                        )
                        if container.returncode == 0 and container.stdout.strip() == "true":
                            break
                    if abusive_broker.poll() is not None:
                        self.fail("first broker exited before claiming the abusive build")
                    time.sleep(0.05)
                else:
                    self.fail("first broker did not claim its job")

                healthy_broker = subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
                )
                children.append(healthy_broker)
                _, healthy_errors = healthy_broker.communicate(timeout=45)
                self.assertEqual(healthy_broker.returncode, 0, healthy_errors)
                built = service.get_build(good_owner, good.shop_id, healthy.id)
                self.assertEqual(built.state, "succeeded", built.model_dump_json())
                self.assertEqual(len(built.report.tests), 9)
                self.assertEqual(
                    service.get_build(bad_owner, bad.shop_id, first.id).state, "running"
                )
                self.assertEqual(
                    service.get_build(bad_owner, bad.shop_id, second.id).state, "queued"
                )
                service.publish(good_owner, good.shop_id, healthy.id, expected_generation=0)
                self.assertEqual(service.catalog(good.shop_id)["shop"]["id"], good.shop_id)

                # End only this test's hostile container, leaving the broker to
                # record failure and release its capacity through the normal path.
                DockerBuilder(environment).remove(first.id)
                _, errors = abusive_broker.communicate(timeout=20)
                self.assertEqual(abusive_broker.returncode, 0, errors)
                self.assertEqual(
                    service.get_build(bad_owner, bad.shop_id, first.id).state, "failed"
                )
                usage = repository.read_quotas()["usage"]["deployment"]
                self.assertEqual(usage["running_builds"], 0)
                self.assertEqual(usage["pending_builds"], 1)
            finally:
                for process in children:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=3)
                    for stream in (process.stdout, process.stderr):
                        stream.close()
                builder = DockerBuilder(environment)
                for build in (first, second, healthy):
                    builder.remove(build.id)


if __name__ == "__main__":
    unittest.main()
