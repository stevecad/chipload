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
from mcp.shared.exceptions import MCPError
from program_support import agent_call, processor
from server_support import IdentityFixture

from chipload.quotas import QuotaPolicy
from chipload.server import Boundary, create_app


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
                guide = await agent.read_resource("chipload://sdk/python")
                self.assertIn("save", json.loads(guide.contents[0].text)["shop_methods"])
                tools = await agent.list_tools()
                self.assertEqual(
                    {"run_python", "read_run", "cancel_run", "discover_shop"},
                    {tool.name for tool in tools.tools},
                )
                result = await agent.call_tool(
                    "discover_shop", {"request": {"shop_id": "thermal-example"}}
                )
                self.assertFalse(result.is_error)
                resource = await agent.read_resource("chipload://shops/thermal-example/catalog")
                self.assertEqual(json.loads(resource.contents[0].text), result.structured_content)
                async with httpx.AsyncClient(trust_env=False) as rest:
                    conventional = await rest.get(self.base + "/v1/shops/thermal-example/catalog")
                    self.assertEqual(conventional.json(), result.structured_content)

    async def test_standalone_streams_finish_without_retaining_shared_capacity(self):
        boundary = self.server.config.app.middleware_stack
        while not isinstance(boundary, Boundary):
            boundary = boundary.app
        boundary.max_active = 1
        async with (
            httpx.AsyncClient(trust_env=False, timeout=2) as transport,
            httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(local_address="127.0.0.2"),
                trust_env=False,
                timeout=2,
            ) as observer,
        ):
            for path in ("/mcp", "/mcp/"):
                for version in (None, "2025-11-25", "2026-07-28"):
                    with self.subTest(path=path, version=version):
                        headers = {"Accept": "text/event-stream"}
                        if version is not None:
                            headers["MCP-Protocol-Version"] = version
                        async with transport.stream("GET", self.base + path, headers=headers) as response:
                            self.assertEqual(response.status_code, 405)
                            self.assertEqual(response.headers["allow"], "POST, OPTIONS")
                            self.assertEqual(await response.aread(), b"")
                        health = await observer.get(self.base + "/healthz")
                        self.assertEqual(health.status_code, 200)
            for method in ("HEAD", "DELETE"):
                response = await transport.request(method, self.base + "/mcp")
                self.assertEqual(response.status_code, 405)
            self.assertEqual((await transport.options(self.base + "/mcp")).status_code, 204)
            metadata = await transport.get(self.base + "/.well-known/oauth-protected-resource/mcp")
            self.assertEqual(metadata.status_code, 200)

    async def test_official_client_and_rest_share_source_and_publication_guards(self):
        headers = {"Authorization": "Bearer " + self.identity.token()}
        async with httpx2.AsyncClient(headers=headers, trust_env=False) as transport:
            async with Client(
                streamable_http_client(self.base + "/mcp", http_client=transport)
            ) as agent:
                identity = await agent.read_resource("chipload://identity/current")
                self.assertEqual(json.loads(identity.contents[0].text)["subject"], "owner")

                async def work():
                    processor(self.operations, self.identity.settings).run_next()

                async def call(name, arguments):
                    return await agent_call(agent, name, arguments["request"], work=work)

                original = await call("source_read", {"request": {"shop_id": "thermal-example"}})
                self.assertFalse(original.is_error)
                saved = original.structured_content
                saved["project"]["files"][0]["content"] += "\n# MCP client edit\n"
                updated = await call(
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
                publication = await call(
                    "publication_read", {"request": {"shop_id": "thermal-example"}}
                )
                generation = publication.structured_content["generation"]
                withdrawn = await call(
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

    async def test_official_client_receives_rate_retry_data_and_can_continue(self):
        clock = [1_792_454_400_000]
        self.operations.limiter.clock = lambda: clock[0]
        async with httpx2.AsyncClient(trust_env=False) as transport:
            async with Client(
                streamable_http_client(self.base + "/mcp", http_client=transport)
            ) as agent:
                await agent.list_tools()
                repository = self.operations.service.repository
                quota = repository.read_quotas()
                quota["policy"]["requests"]["peer"] = {"per_minute": 60, "burst": 1}
                repository.set_quotas(
                    QuotaPolicy.model_validate(quota["policy"]),
                    expected_generation=quota["generation"],
                )
                arguments = {"request": {"shop_id": "thermal-example"}}
                result = await agent.call_tool("discover_shop", arguments)
                self.assertFalse(result.is_error)
                with self.assertRaises(MCPError) as failure:
                    await agent.call_tool("discover_shop", arguments)
                self.assertEqual(failure.exception.code, 429)
                self.assertEqual(failure.exception.data["status"], 429)
                self.assertEqual(failure.exception.data["error"]["limit"], "peer")
                self.assertEqual(failure.exception.data["error"]["retry_after"], 1)
                clock[0] += 1000
                recovered = await agent.call_tool("discover_shop", arguments)
                self.assertEqual(recovered.structured_content, result.structured_content)


if __name__ == "__main__":
    unittest.main()
