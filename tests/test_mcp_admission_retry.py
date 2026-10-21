"""Functional MCP fixtures retry only a definite refusal before execution."""

import unittest
from unittest.mock import patch

import httpx2
from program_support import AdmissionRetryTransport


def refusal():
    return httpx2.Response(503, headers={"Retry-After": "1"}, json={
        "jsonrpc": "2.0", "id": None,
        "error": {"code": 503, "data": {"status": 503, "error": {
            "code": "request_capacity", "retry_after": 1,
        }}},
    })


class AdmissionRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_refusal_preserves_exact_mutation_and_durable_key(self):
        requests = []

        def handle(request):
            requests.append((request.method, request.url, request.headers, request.content))
            return refusal() if len(requests) == 1 else httpx2.Response(200, json={"ok": True})

        with patch("program_support.asyncio.sleep") as sleep:
            async with httpx2.AsyncClient(
                transport=AdmissionRetryTransport(httpx2.MockTransport(handle)),
            ) as client:
                response = await client.post("https://shop.example/mcp", json={
                    "method": "tools/call", "params": {"name": "run_python", "arguments": {
                        "request": {"request_key": "one-run", "code": "result=1"},
                    }},
                })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1])
        sleep.assert_awaited_once_with(1)

    async def test_persistent_refusal_is_returned_after_bounded_wait(self):
        requests = []

        def handle(request):
            requests.append(request)
            return refusal()

        with patch("program_support.asyncio.sleep") as sleep:
            async with httpx2.AsyncClient(
                transport=AdmissionRetryTransport(httpx2.MockTransport(handle)),
            ) as client:
                response = await client.post("https://shop.example/mcp", content=b"same request")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(requests), 4)
        self.assertEqual(sleep.await_count, 3)

    async def test_other_failures_and_successful_tool_errors_are_not_retried(self):
        for response in (
            httpx2.Response(503, json={"error": {"code": "request_capacity"}}),
            httpx2.Response(503, content=b"gateway unavailable"),
            httpx2.Response(429, json={"error": {"code": "request_rate"}}),
            httpx2.Response(200, json={"result": {"isError": True}}),
        ):
            with self.subTest(status=response.status_code, body=response.content):
                requests = []

                def handle(request):
                    requests.append(request)
                    return response

                async with httpx2.AsyncClient(
                    transport=AdmissionRetryTransport(httpx2.MockTransport(handle)),
                ) as client:
                    received = await client.post("https://shop.example/mcp", content=b"one request")
                self.assertEqual(received.content, response.content)
                self.assertEqual(len(requests), 1)

    async def test_lost_response_is_never_retried(self):
        requests = []

        def handle(request):
            requests.append(request)
            raise httpx2.ReadError("response lost after possible commit")

        async with httpx2.AsyncClient(
            transport=AdmissionRetryTransport(httpx2.MockTransport(handle)),
        ) as client:
            with self.assertRaises(httpx2.ReadError):
                await client.post("https://shop.example/mcp", content=b"one mutation")
        self.assertEqual(len(requests), 1)
