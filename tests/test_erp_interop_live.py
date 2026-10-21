"""Actual source build and MCP bridge calls with a separate simulated ERP record."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from erp_support import ERPFixture
from fastapi.testclient import TestClient
from program_support import wire
from server_support import IdentityFixture

from chipload.auth import TokenVerifier
from chipload.executor import SimulatedAdapter
from chipload.lifecycle import now
from chipload.program_runner import ProgramProcessor
from chipload.program_sandbox import DockerProgramRunner
from chipload.sandbox import DockerBuilder, WorkerEnvironment
from chipload.server import create_app
from examples import erp_shop


class ExternalStockFixture(SimulatedAdapter):
    """Only the trusted test adapter reads this upstream file; it never writes it."""

    def __init__(self, fixture, path):
        super().__init__(fixture.repository, (), clock=lambda: fixture.at)
        self.fixture, self.path, self.reads = fixture, path, 0

    def execute(self, context):
        source = json.loads(self.path.read_text())
        self.reads += 1
        self.outputs = self.fixture.outputs(version=source["revision"], count=source["available-units"],
                                            at=datetime.fromisoformat(source["observed-at"]))
        return super().execute(context)


@unittest.skipUnless(os.environ.get("CHIPLOAD_WORKER_TESTS") == "1", "requires rebuilt worker image")
class ERPInteropLiveTests(unittest.TestCase):
    def test_external_read_retry_sync_freshness_and_public_privacy_through_rest_and_mcp(self):
        environment = WorkerEnvironment.inspect(Path(".local/worker-image").read_text().strip())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            f = ERPFixture(root / "data" / "shop.db", builder=DockerBuilder(environment), at=now())
            upstream = root / "external-erp-stock.json"
            upstream.write_text(json.dumps({"revision": "stock-v1", "available-units": "5", "observed-at": f.at.isoformat()}))
            original = upstream.read_bytes()
            adapter = ExternalStockFixture(f, upstream)
            f.runner.adapters["erp"] = adapter
            identity = IdentityFixture(root / "identity")
            self.addCleanup(identity.client.close)
            settings = identity.settings.model_copy(update={"database": f.repository.path, "bindings": (f.owner, f.agent)})
            app = create_app(settings, operations=f.operations, verifier=TokenVerifier(settings, client=identity.client))
            runner = ProgramProcessor(f.repository, DockerProgramRunner(environment, f.repository), f.principals.get)
            headers = {"Authorization": "Bearer " + identity.token("erp-agent")}
            shop_path = f"/v1/shops/{erp_shop.SHOP}"
            with TestClient(app, base_url="http://localhost:8088") as http:
                self.assertEqual(http.get(shop_path + "/source", headers=headers).status_code, 403)
                submitted = http.post(shop_path + f"/operations/{erp_shop.READ_OPERATION}/executions", headers=headers, json={
                    "shop_id": erp_shop.SHOP, "operation_id": erp_shop.READ_OPERATION,
                    "request_key": "erp-read-1", "expected_generation": 1, "mode": "simulated",
                    "inputs": [{"name": "record-reference", "value": erp_shop.STOCK_RECORD},
                               {"name": "purpose", "value": "shop-readiness"}],
                })
                self.assertEqual(submitted.status_code, 202, submitted.text)
                execution_id = submitted.json()["id"]
                self.assertTrue(f.runner.run_next())

                def code(key, program, inputs=None):
                    response = wire(http, "run_python", {
                        "shop_id": erp_shop.SHOP, "request_key": key, "code": program,
                        "files": [{"path": "bridge.py", "content": Path(erp_shop.__file__).read_text()}],
                        "inputs": inputs or {},
                    }, headers).json()["result"]
                    self.assertFalse(response.get("isError"), response)
                    self.assertTrue(runner.run_next())
                    result = f.operations.programs.read(f.agent, erp_shop.SHOP, response["structuredContent"]["id"])
                    self.assertEqual(result.state, "succeeded", result)
                    return result.value

                receipt = code("retry-read", "from bridge import read_stock, SHOP\nresult=read_stock(client.shop(SHOP),request_key='erp-read-1',mode='simulated')")
                self.assertEqual(receipt["id"], execution_id)
                self.assertFalse(f.runner.run_next())
                self.assertEqual(adapter.reads, 1)
                updated = code("sync", "from bridge import sync_stock, SHOP\nresult=sync_stock(client.shop(SHOP),inputs['id'])", {"id": execution_id})
                self.assertEqual(updated["state"], "updated")
                observed = http.get(shop_path + "/observations", headers=headers).json()
                self.assertEqual(observed["observations"][0]["external"]["revision"], "stock-v1")
                self.assertEqual(observed["observations"][0]["source"], "simulated")
                self.assertEqual(datetime.fromisoformat(observed["observations"][0]["time"].replace("Z", "+00:00")), f.at)
                query = erp_shop.request().model_dump(mode="json")
                public = http.post(shop_path + "/evaluate", json={"shop_id": erp_shop.SHOP, "query": query}).json()
                self.assertEqual(public["outcome"], "supported")
                agent = code("discover", "from bridge import SHOP\nresult=client.shop(SHOP).evaluate(inputs['query'])", {"query": query})
                self.assertEqual(agent["outcome"], public["outcome"])
                self.assertNotIn(erp_shop.STOCK_RECORD, json.dumps(public))
                self.assertNotIn(erp_shop.SYSTEM, json.dumps(public))
                self.assertNotIn("stock-v1", http.get(shop_path + "/catalog").text)
                self.assertEqual(http.get(shop_path + "/observations").status_code, 401)
                f.at += timedelta(seconds=61)
                expired = http.post(shop_path + "/evaluate", json={"shop_id": erp_shop.SHOP, "query": query}).json()
                self.assertEqual(expired["outcome"], "unknown")
                self.assertEqual(expired["freshness"][0]["state"], "stale")
                self.assertEqual(upstream.read_bytes(), original)
                self.assertEqual(adapter.reads, 1)


if __name__ == "__main__":
    unittest.main()
