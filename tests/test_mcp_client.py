import asyncio
import json
import socket
import tempfile
import threading
import unittest

import httpx
import httpx2
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from server_support import IdentityFixture

from chipload.server import create_app


class MCPClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.base = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
        self.identity = IdentityFixture(self.directory.name, base=self.base)
        self.operations = self.identity.operations()
        app = create_app(
            self.identity.settings, operations=self.operations, verifier=self.identity.verifier
        )
        self.server = uvicorn.Server(
            uvicorn.Config(
                app,
                log_level="warning",
                access_log=False,
                proxy_headers=False,
            )
        )
        self.thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True
        )
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            await asyncio.sleep(0.05)
        self.assertTrue(self.server.started)

    async def asyncTearDown(self):
        self.server.should_exit = True
        await asyncio.to_thread(self.thread.join, 10)
        self.assertFalse(self.thread.is_alive())
        self.identity.client.close()
        self.socket.close()
        self.directory.cleanup()

    async def test_official_client_discovers_public_tools_and_resources(self):
        async with httpx2.AsyncClient(trust_env=False) as transport:
            async with Client(
                streamable_http_client(self.base + "/mcp", http_client=transport)
            ) as agent:
                tools = await agent.list_tools()
                self.assertIn("source_save", {tool.name for tool in tools.tools})
                result = await agent.call_tool(
                    "shop_catalog", {"request": {"shop_id": "thermal-example"}}
                )
                self.assertFalse(result.is_error)
                resource = await agent.read_resource("chipload://shops/thermal-example/catalog")
                self.assertEqual(json.loads(resource.contents[0].text), result.structured_content)
                async with httpx.AsyncClient(trust_env=False) as rest:
                    conventional = await rest.get(self.base + "/v1/shops/thermal-example/catalog")
                    self.assertEqual(conventional.json(), result.structured_content)

    async def test_official_client_and_rest_share_source_and_publication_guards(self):
        headers = {"Authorization": "Bearer " + self.identity.token()}
        async with httpx2.AsyncClient(headers=headers, trust_env=False) as transport:
            async with Client(
                streamable_http_client(self.base + "/mcp", http_client=transport)
            ) as agent:
                original = await agent.call_tool(
                    "source_read", {"request": {"shop_id": "thermal-example"}}
                )
                self.assertFalse(original.is_error)
                saved = original.structured_content
                saved["project"]["files"][0]["content"] += "\n# MCP client edit\n"
                updated = await agent.call_tool(
                    "source_save",
                    {
                        "request": {
                            "project": saved["project"],
                            "expected_head": saved["revision"],
                        }
                    },
                )
                self.assertFalse(updated.is_error, updated)
                async with httpx.AsyncClient(headers=headers, trust_env=False) as rest:
                    conventional = await rest.get(self.base + "/v1/shops/thermal-example/source")
                    self.assertEqual(conventional.json(), updated.structured_content)
                publication = await agent.call_tool(
                    "publication_read", {"request": {"shop_id": "thermal-example"}}
                )
                generation = publication.structured_content["generation"]
                withdrawn = await agent.call_tool(
                    "publication_change",
                    {
                        "request": {
                            "shop_id": "thermal-example",
                            "action": "withdraw",
                            "expected_generation": generation,
                        }
                    },
                )
                self.assertFalse(withdrawn.is_error)
                async with httpx.AsyncClient(trust_env=False) as rest:
                    self.assertEqual(
                        (
                            await rest.get(self.base + "/v1/shops/thermal-example/catalog")
                        ).status_code,
                        404,
                    )


if __name__ == "__main__":
    unittest.main()
