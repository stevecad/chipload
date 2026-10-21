"""Shared ERP interop fixtures; all upstream data and effects are simulated."""

from execution_support import CallableBuilder

from chipload.access import ACTIONS, Grant, Principal
from chipload.client import Client, ServiceError
from chipload.execution import ExecutionService
from chipload.execution_model import AdapterBinding
from chipload.executor import OperationRunner, SimulatedAdapter
from chipload.lifecycle import BuildProcessor, Repository, ShopService
from chipload.model import Fact, quantity
from chipload.operations import Operations
from chipload.published import PublishedService
from chipload.service_errors import problem
from examples import erp_shop


class ERPFixture:
    def __init__(self, path, *, builder=None, at=erp_shop.AT):
        self.at = at
        self.repository = Repository(path)
        self.service = ShopService(self.repository)
        self.owner = Principal(subject="owner", grants=(Grant(shop_id=erp_shop.SHOP, actions=ACTIONS),))
        self.agent = Principal(subject="erp-agent", grants=(Grant(shop_id=erp_shop.SHOP, actions=(
            "code:run", "code:read", "code:cancel", "operation:execute", "operation:read",
            "observations:read", "observations:write", "evaluation:read"), operations=(erp_shop.READ_OPERATION,)),))
        source = erp_shop.project()
        self.service.save_source(self.owner, source, expected_head=None)
        job = self.service.request_build(self.owner, erp_shop.SHOP, source.revision)
        BuildProcessor(self.repository, builder or CallableBuilder(erp_shop.define_shop())).run(job.id)
        self.build = self.service.get_build(self.owner, erp_shop.SHOP, job.id)
        if self.build.state != "succeeded":
            raise AssertionError(self.build.report)
        self.service.publish(self.owner, erp_shop.SHOP, job.id, expected_generation=0)
        self.operations = Operations(self.service, PublishedService(self.service, clock=lambda: self.at))
        self.operations.executions = ExecutionService(self.repository, clock=lambda: self.at)
        self.binding = AdapterBinding(shop_id=erp_shop.SHOP, operation_id=erp_shop.READ_OPERATION,
                                      model_sha256=self.build.report.artifact.model_sha256,
                                      adapter_id="erp", mode="simulated")
        self.operations.executions.bind(self.binding, expected_generation=0)
        self.adapter = SimulatedAdapter(self.repository, self.outputs(), clock=lambda: self.at)
        self.principals = {p.subject: p for p in (self.owner, self.agent)}
        self.runner = OperationRunner(self.repository, {"erp": self.adapter}, self.principals.get, clock=lambda: self.at)
        self.shop = Client(self.call).shop(erp_shop.SHOP)

    def outputs(self, *, version="stock-v1", count="5", at=None):
        return (
            Fact(name="record-reference", value=erp_shop.STOCK_RECORD),
            Fact(name="record-revision", value=version),
            Fact(name="observed-at", value=(at or self.at).isoformat()),
            *((Fact(name="available-units", value=quantity(count, "1")),) if count is not None else ()),
        )

    def call(self, operation, request):
        try:
            return self.operations.call(operation, self.agent, request)
        except Exception as error:
            status, detail = problem(error)
            raise ServiceError(status, detail) from None

    def read_stock(self, key="read-stock"):
        job = erp_shop.read_stock(self.shop, request_key=key, mode="simulated")
        self.runner.run_next()
        return self.shop.execution(erp_shop.READ_OPERATION, job["id"])
