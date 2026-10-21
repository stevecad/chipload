"""Data-only unit fixtures for the callable shop; live tests build its actual source."""

from datetime import timedelta

from chipload.access import ACTIONS, Grant, Principal
from chipload.artifact import BuildIdentity, compile_shop
from chipload.execution import ExecutionService
from chipload.execution_model import AdapterBinding, ExecutionRequest
from chipload.executor import OperationRunner, SimulatedAdapter
from chipload.lifecycle import BuildProcessor, Repository, ShopService
from chipload.project import BuildReport, run_cases
from chipload.sandbox import WorkerEnvironment
from examples.callable_shop import AT, SIMULATORS, define_shop, inputs, project


class CallableBuilder:
    seconds = 1
    environment = WorkerEnvironment("sha256:" + "a" * 64, "b" * 64, "c" * 64, "3.12.15")

    def __init__(self, model=None):
        self.model = model or define_shop()

    def remove(self, job_id):
        pass  # This fixture never starts a container.

    def build(self, source, *, job_id=None):
        artifact = compile_shop(
            self.model,
            BuildIdentity(
                source_sha256=source.revision,
                dependencies_sha256=self.environment.dependencies_sha256,
                sdk_sha256=self.environment.sdk_sha256,
                runtime="test-fixture",
            ),
        )
        return BuildReport(
            source_revision=source.revision,
            worker_image=self.environment.image,
            status="succeeded",
            artifact=artifact,
            tests=run_cases(artifact, source.tests),
        )


class ExecutionFixture:
    def __init__(self, path, model=None):
        self.repository = Repository(path)
        self.shop = "callable-example"
        self.owner = Principal(subject="owner", grants=(Grant(shop_id=self.shop, actions=ACTIONS),))
        self.service = ShopService(self.repository)
        source = project()
        saved = self.service.save_source(self.owner, source, expected_head=None)
        requested = self.service.request_build(self.owner, self.shop, saved.revision)
        BuildProcessor(self.repository, CallableBuilder(model)).run(requested.id)
        self.build = self.service.get_build(self.owner, self.shop, requested.id)
        self.service.publish(self.owner, self.shop, self.build.id, expected_generation=0)
        self.at = AT + timedelta(days=1)
        self.execution = ExecutionService(self.repository, clock=lambda: self.at)
        self.adapters = {
            name: SimulatedAdapter(self.repository, output, clock=lambda: self.at)
            for name, output in SIMULATORS.items()
        }
        self.principals = {"owner": self.owner}
        self.runner = OperationRunner(
            self.repository, self.adapters, self.principals.get, clock=lambda: self.at
        )
        for operation, adapter in (
            ("make-sample", "machine"),
            ("review-document", "review"),
            ("inspect-sample", "inspection"),
            ("outside-process", "external"),
        ):
            self.execution.bind(
                AdapterBinding(
                    shop_id=self.shop,
                    operation_id=operation,
                    model_sha256=self.build.report.artifact.model_sha256,
                    adapter_id=adapter,
                    mode="simulated",
                ),
                expected_generation=0,
            )

    def request(self, key="first", **changes):
        values = dict(
            shop_id=self.shop,
            operation_id="make-sample",
            expected_generation=1,
            request_key=key,
            mode="simulated",
            inputs=inputs(),
        )
        return ExecutionRequest.model_validate({**values, **changes})

    def submit(self, key="first", **changes):
        return self.execution.submit(self.owner, self.request(key, **changes))

    def read(self, identifier, operation="make-sample"):
        return self.execution.read(self.owner, self.shop, operation, identifier)
