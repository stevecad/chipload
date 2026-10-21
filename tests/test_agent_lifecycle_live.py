"""An MCP client authors a shop while a separate Docker broker builds it."""

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import httpx
import httpx2
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from program_support import agent_call
from server_support import IdentityFixture

from chipload.lifecycle import Repository, ShopService
from chipload.operations import Operations
from chipload.server import create_app
from examples.export_project import project


@unittest.skipUnless(
    os.environ.get("CHIPLOAD_WORKER_TESTS") == "1", "requires a built Docker worker"
)
class AgentLifecycleLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_author_repairs_build_publishes_updates_rolls_back_and_withdraws(self):
        with tempfile.TemporaryDirectory() as directory:
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            base = f"http://127.0.0.1:{sock.getsockname()[1]}"
            identity = IdentityFixture(Path(directory) / "data", base=base)
            operations = Operations(ShopService(Repository(identity.settings.database)))
            app = create_app(identity.settings, operations=operations, verifier=identity.verifier)
            server = uvicorn.Server(
                uvicorn.Config(app, access_log=False, log_level="warning", proxy_headers=False)
            )
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                for _ in range(100):
                    if server.started:
                        break
                    await asyncio.sleep(0.05)
                self.assertTrue(server.started)
                image = (
                    os.environ.get("CHIPLOAD_WORKER_IMAGE")
                    or Path(".local/worker-image").read_text().strip()
                )

                settings_path = Path(directory) / "broker-settings.json"
                settings_path.write_text(identity.settings.model_dump_json())
                settings_path.chmod(0o600)

                async def work():
                    completed = await asyncio.to_thread(
                        subprocess.run,
                        [
                            sys.executable,
                            "-m",
                            "chipload.broker",
                            "--database",
                            str(identity.settings.database),
                            "--image",
                            image,
                            "--settings",
                            str(settings_path),
                            "--once",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)

                async with httpx2.AsyncClient(
                    headers={"Authorization": "Bearer " + identity.token()}, trust_env=False
                ) as transport:
                    async with Client(
                        streamable_http_client(base + "/mcp", http_client=transport)
                    ) as agent:

                        async def call(name, values):
                            result = await agent_call(agent, name, values, work=work)
                            self.assertFalse(result.is_error, result)
                            return result.structured_content

                        reference = await call("sdk_reference", {})
                        self.assertIn("source_project", reference["schemas"])
                        valid = project().model_dump(mode="json")
                        broken = {
                            **valid,
                            "files": [{"path": "thermal.py", "content": "def broken(:\n"}],
                        }
                        saved = await call(
                            "source_save", {"project": broken, "expected_head": None}
                        )
                        queued = await call(
                            "build_request",
                            {
                                "shop_id": "thermal-example",
                                "revision": saved["revision"],
                                "request_key": "broken",
                            },
                        )
                        self.assertEqual(queued["state"], "queued")
                        await work()
                        failed = await call(
                            "build_inspect",
                            {"shop_id": "thermal-example", "build_id": queued["id"]},
                        )
                        self.assertEqual(failed["state"], "failed")
                        self.assertEqual(
                            failed["report"]["diagnostics"][0]["path"], ["thermal.py", "1"]
                        )

                        repaired = await call(
                            "source_save", {"project": valid, "expected_head": saved["revision"]}
                        )
                        first = await call(
                            "build_request",
                            {
                                "shop_id": "thermal-example",
                                "revision": repaired["revision"],
                                "request_key": "repaired",
                            },
                        )
                        await work()
                        built = await call(
                            "build_inspect", {"shop_id": "thermal-example", "build_id": first["id"]}
                        )
                        self.assertEqual(
                            built["state"], "succeeded", built["report"]["diagnostics"]
                        )
                        self.assertEqual(len(built["report"]["tests"]), 9)
                        await call(
                            "publication_change",
                            {
                                "shop_id": "thermal-example",
                                "action": "publish",
                                "build_id": first["id"],
                                "expected_generation": 0,
                            },
                        )
                        async with httpx.AsyncClient(base_url=base, trust_env=False) as rest:
                            original = (await rest.get("/v1/shops/thermal-example/catalog")).json()
                            self.assertEqual(
                                original, await call("shop_catalog", {"shop_id": "thermal-example"})
                            )
                            valid["files"][0]["content"] = valid["files"][0]["content"].replace(
                                "Thermal Processing Example", "Updated through MCP"
                            )
                            edited = await call(
                                "source_save",
                                {"project": valid, "expected_head": repaired["revision"]},
                            )
                            second = await call(
                                "build_request",
                                {
                                    "shop_id": "thermal-example",
                                    "revision": edited["revision"],
                                    "request_key": "updated",
                                },
                            )
                            await work()
                            await call(
                                "publication_change",
                                {
                                    "shop_id": "thermal-example",
                                    "action": "publish",
                                    "build_id": second["id"],
                                    "expected_generation": 1,
                                },
                            )
                            self.assertEqual(
                                (await rest.get("/v1/shops/thermal-example/catalog")).json()[
                                    "shop"
                                ]["name"],
                                "Updated through MCP",
                            )
                            await call(
                                "publication_change",
                                {
                                    "shop_id": "thermal-example",
                                    "action": "rollback",
                                    "build_id": first["id"],
                                    "expected_generation": 2,
                                },
                            )
                            self.assertEqual(
                                (await rest.get("/v1/shops/thermal-example/catalog")).json(),
                                original,
                            )
                            await call(
                                "publication_change",
                                {
                                    "shop_id": "thermal-example",
                                    "action": "withdraw",
                                    "expected_generation": 3,
                                },
                            )
                            self.assertEqual(
                                (await rest.get("/v1/shops/thermal-example/catalog")).status_code,
                                404,
                            )
            finally:
                server.should_exit = True
                await asyncio.to_thread(thread.join, 10)
                identity.client.close()
                sock.close()
                self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
