"""Transport regression helpers. FixtureRunner never executes submitted Python.

Live suites use the real container runner. This exact fixture script composes one
SDK call for parity checks; product clients should compose complete workflows.
"""

import asyncio
import json
import uuid

import httpx
from mcp_types import CallToolResult

from chipload.program_runner import ProgramProcessor

META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}
PUBLIC = {
    "shop_catalog": "catalog",
    "shop_list": "page",
    "shop_inspect": "inspect",
    "shop_evaluate": "evaluate",
    "shop_variables": "variables",
}
SCRIPT = """from chipload.client import ServiceError
try:
    result = {'ok': True, 'value': client.call(inputs['operation'], inputs['request'])}
except ServiceError as error:
    result = {'ok': False, 'status': error.status, 'detail': error.detail}
"""


class FixtureRunner:
    def run(self, identifier, request, dispatch, active):
        assert request.code == SCRIPT, "Only the exact trusted parity fixture is supported"
        assert active()
        return {"value": dispatch(request.inputs["operation"], request.inputs["request"])}

    def remove(self, identifier):
        pass


def processor(operations, settings):
    return ProgramProcessor(
        operations.service.repository,
        FixtureRunner(),
        lambda subject: next((p for p in settings.bindings if p.subject == subject), None),
        operations=operations,
    )


def discovery(name, body):
    return {
        "shop_id": body["shop_id"],
        "action": PUBLIC[name],
        "arguments": {k: v for k, v in body.items() if k != "shop_id"},
    }


def submission(name, body):
    return {
        "shop_id": body.get("shop_id") or body.get("project", {}).get("shop_id", "thermal-example"),
        "request_key": uuid.uuid4().hex,
        "code": SCRIPT,
        "inputs": {"operation": name, "request": body},
    }


def envelope(value):
    return {
        "isError": not value["ok"],
        "structuredContent": value["value"]
        if value["ok"]
        else {"status": value["status"], **value["detail"]},
        "content": [],
    }


def wire(client, name, body, headers=None, *, method="tools/call"):
    return client.post(
        "/mcp",
        headers={
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": method,
            "Mcp-Name": name,
            **(headers or {}),
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": (
                {"name": name, "arguments": {"request": body}, "_meta": META}
                if method == "tools/call"
                else {"uri": name, "_meta": META}
            ),
        },
    )


def call(client, name, body, headers=None, *, worker=None):
    if name in PUBLIC:
        return wire(client, "discover_shop", discovery(name, body), headers)
    if name in {"identity_read", "sdk_reference"}:
        return wire(
            client,
            "chipload://identity/current"
            if name == "identity_read"
            else "chipload://sdk/reference",
            {},
            headers,
            method="resources/read",
        )
    response = wire(client, "run_python", submission(name, body), headers)
    if response.status_code != 200 or response.json().get("result", {}).get("isError"):
        return response
    run = response.json()["result"]["structuredContent"]
    assert worker is not None
    assert worker.run_next()
    response = wire(client, "read_run", {"shop_id": run["shop_id"], "run_id": run["id"]}, headers)
    run = response.json()["result"]["structuredContent"]
    assert run["state"] == "succeeded", run
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": envelope(run["value"])})


async def agent_call(agent, name, body, *, work=None):
    if name in PUBLIC:
        return await agent.call_tool("discover_shop", {"request": discovery(name, body)})
    if name in {"identity_read", "sdk_reference"}:
        resource = await agent.read_resource(
            "chipload://identity/current" if name == "identity_read" else "chipload://sdk/reference"
        )
        return CallToolResult(structured_content=json.loads(resource.contents[0].text), content=[])
    response = await agent.call_tool("run_python", {"request": submission(name, body)})
    if response.is_error:
        return response
    run = response.structured_content
    if work:
        await work()
    for _ in range(180):
        response = await agent.call_tool(
            "read_run", {"request": {"shop_id": run["shop_id"], "run_id": run["id"]}}
        )
        if response.is_error:
            return response
        run = response.structured_content
        if run["state"] not in {"queued", "running"}:
            assert run["state"] == "succeeded", run
            value = envelope(run["value"])
            return CallToolResult(
                is_error=value["isError"], structured_content=value["structuredContent"], content=[]
            )
        await asyncio.sleep(0.5)
    raise AssertionError("Python run did not settle")
