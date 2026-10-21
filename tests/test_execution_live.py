"""Real TLS/Keycloak, isolated Python build, and a separate simulated executor."""

import asyncio
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from program_support import AdmissionRetryTransport, agent_call
from tls_support import ROOT

from chipload.execution import ExecutionService
from chipload.execution_model import AdapterBinding
from chipload.lifecycle import Repository
from examples.callable_shop import SIMULATORS, inputs, project


@unittest.skipUnless(
    os.environ.get("CHIPLOAD_OPERATION_TESTS") == "1", "requires worker, runtime, and TLS images"
)
class ExecutionLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_shop_agent_authors_and_calls_four_operations_over_tls(self):
        with tempfile.TemporaryDirectory(prefix="chipload-operations-") as temporary:
            root = Path(temporary)
            state = root / "state"
            listeners = [socket.socket(), socket.socket()]
            for listener in listeners:
                listener.bind(("127.0.0.1", 0))
            port, admin_port = (listener.getsockname()[1] for listener in listeners)
            for listener in listeners:
                listener.close()
            name = "chipload-operations-" + uuid.uuid4().hex[:12]
            cli = [sys.executable, str(ROOT / "scripts/tls-stack"), "--state", str(state)]

            async def run(*arguments, timeout=240, expected=0):
                result = await asyncio.to_thread(
                    subprocess.run, list(arguments), capture_output=True, text=True, timeout=timeout
                )
                self.assertEqual(result.returncode, expected, result.stderr)
                return result.stdout.strip()

            await run(
                *cli,
                "init",
                "--local",
                "--port",
                str(port),
                "--admin-port",
                str(admin_port),
                "--name",
                name,
                "--shop",
                "callable-example",
            )
            try:
                await run(*cli, "up")
                connection = json.loads((state / "client.json").read_bytes())
                base = connection["resource_url"][:-4]
                context = ssl.create_default_context(cafile=connection["ca_file"])
                async with httpx.AsyncClient(verify=context, trust_env=False, timeout=20) as rest:

                    async def post(path, **arguments):
                        for _ in range(6):
                            response = await rest.post(path, **arguments)
                            if response.status_code != 429:
                                return response
                            await asyncio.sleep(min(10, int(response.headers["Retry-After"])))
                        self.fail("REST request capacity did not recover")

                    authenticated = await rest.post(
                        connection["issuer"] + "/protocol/openid-connect/token",
                        data={
                            "grant_type": "client_credentials",
                            "client_id": connection["client_id"],
                            "client_secret": Path(connection["client_secret_file"]).read_text(),
                        },
                    )
                    self.assertEqual(authenticated.status_code, 200)
                    headers = {"Authorization": "Bearer " + authenticated.json()["access_token"]}
                    async with httpx2.AsyncClient(
                        headers=headers, trust_env=False,
                        transport=AdmissionRetryTransport(httpx2.AsyncHTTPTransport(
                            verify=context, trust_env=False,
                        )),
                    ) as transport:
                        async with Client(
                            streamable_http_client(
                                connection["resource_url"], http_client=transport
                            )
                        ) as agent:

                            async def call(tool, request):
                                for _ in range(6):
                                    result = await agent_call(agent, tool, request)
                                    payload = result.structured_content or {}
                                    if result.is_error and payload.get("status") == 429:
                                        await asyncio.sleep(
                                            min(10, payload["error"]["retry_after"])
                                        )
                                        continue
                                    self.assertFalse(result.is_error, result)
                                    return result.structured_content
                                self.fail("MCP request capacity did not recover")

                            saved = await call(
                                "source_save",
                                {
                                    "project": project().model_dump(mode="json"),
                                    "expected_head": None,
                                },
                            )
                            job = await call(
                                "build_request",
                                {
                                    "shop_id": "callable-example",
                                    "revision": saved["revision"],
                                    "request_key": "build",
                                },
                            )
                            for _ in range(150):
                                built = await call(
                                    "build_inspect",
                                    {"shop_id": "callable-example", "build_id": job["id"]},
                                )
                                if built["state"] in {"failed", "succeeded"}:
                                    break
                                await asyncio.sleep(0.3)
                            self.assertEqual(built["state"], "succeeded", built["report"])
                            self.assertEqual(len(built["report"]["tests"]), 4)
                            await call(
                                "publication_change",
                                {
                                    "shop_id": "callable-example",
                                    "action": "publish",
                                    "build_id": job["id"],
                                    "expected_generation": 0,
                                },
                            )
                            catalog = await rest.get(base + "/v1/shops/callable-example/catalog")
                            self.assertEqual(catalog.status_code, 200)
                            self.assertEqual(len(catalog.json()["operations"]), 4)
                            repository = Repository(state / "data/chipload.db")
                            executions = ExecutionService(repository)
                            settings = json.loads((state / "runtime.json").read_bytes())
                            settings["database"] = str(repository.path)
                            settings_path, simulators = (
                                root / "executor.json",
                                root / "simulators.json",
                            )
                            for path, value in (
                                (settings_path, settings),
                                (
                                    simulators,
                                    {
                                        "adapters": [
                                            {
                                                "id": key,
                                                "outputs": [
                                                    fact.model_dump(mode="json") for fact in facts
                                                ],
                                            }
                                            for key, facts in SIMULATORS.items()
                                        ]
                                    },
                                ),
                            ):
                                path.touch(mode=0o600)
                                path.write_text(json.dumps(value))
                            executor = [
                                sys.executable,
                                "-m",
                                "chipload.executor",
                                "--settings",
                                str(settings_path),
                                "--simulators",
                                str(simulators),
                                "--once",
                            ]
                            common = [fact.model_dump(mode="json") for fact in inputs()[1:]]
                            operations = (
                                (
                                    "review-document",
                                    "review",
                                    [{"name": "document", "value": "drawing-1"}, *common],
                                ),
                                (
                                    "make-sample",
                                    "machine",
                                    [fact.model_dump(mode="json") for fact in inputs()],
                                ),
                                ("inspect-sample", "inspection", common),
                                (
                                    "outside-process",
                                    "external",
                                    [{"name": "part-reference", "value": "sample-1"}, *common],
                                ),
                            )
                            first_request = None
                            first_id = None
                            for operation, adapter, facts in operations:
                                body = {
                                    "shop_id": "callable-example",
                                    "operation_id": operation,
                                    "expected_generation": 1,
                                    "mode": "simulated",
                                    "request_key": operation,
                                    "inputs": facts,
                                }
                                path = (
                                    base
                                    + f"/v1/shops/callable-example/operations/{operation}/executions"
                                )
                                # Publishing source alone cannot provision an adapter.
                                self.assertEqual(
                                    (await post(path, json=body, headers=headers)).status_code,
                                    409,
                                )
                                executions.bind(
                                    AdapterBinding(
                                        shop_id="callable-example",
                                        operation_id=operation,
                                        model_sha256=built["report"]["artifact"]["model_sha256"],
                                        adapter_id=adapter,
                                        mode="simulated",
                                    ),
                                    expected_generation=0,
                                )
                                submitted = await call("operation_submit", body)
                                duplicate = await post(path, json=body, headers=headers)
                                self.assertEqual(duplicate.status_code, 202)
                                self.assertEqual(duplicate.json(), submitted)
                                self.assertEqual(
                                    json.loads(await run(*executor))["dispatched"], True
                                )
                                completed = await rest.get(
                                    path + "/" + submitted["id"], headers=headers
                                )
                                self.assertEqual(
                                    completed.json()["state"], "succeeded", completed.text
                                )
                                self.assertEqual(completed.json()["mode"], "simulated")
                                self.assertEqual(
                                    completed.json(),
                                    await call(
                                        "operation_execution_read",
                                        {
                                            "shop_id": "callable-example",
                                            "operation_id": operation,
                                            "execution_id": submitted["id"],
                                        },
                                    ),
                                )
                                self.assertEqual(
                                    completed.json()["result"]["outputs"],
                                    [fact.model_dump(mode="json") for fact in SIMULATORS[adapter]],
                                )
                                if first_request is None:
                                    first_request, first_id = body, submitted["id"]
                            self.assertFalse(json.loads(await run(*executor))["dispatched"])
                            self.assertEqual(
                                (await call("operation_submit", first_request))["id"], first_id
                            )
                            cancelled = await call(
                                "operation_submit", {**first_request, "request_key": "cancel"}
                            )
                            result = await call(
                                "operation_execution_cancel",
                                {
                                    "shop_id": "callable-example",
                                    "operation_id": first_request["operation_id"],
                                    "execution_id": cancelled["id"],
                                },
                            )
                            self.assertEqual(result["state"], "cancelled")
                            denied_path = (
                                base
                                + "/v1/shops/callable-example/operations/review-document/executions"
                            )
                            self.assertEqual(
                                (
                                    await post(
                                        denied_path,
                                        json={
                                            **first_request,
                                            "request_key": "live",
                                            "mode": "live",
                                        },
                                        headers=headers,
                                    )
                                ).status_code,
                                409,
                            )
                            self.assertEqual(
                                (await post(denied_path, json=first_request)).status_code, 401
                            )
                            self.assertEqual(
                                (
                                    await post(
                                        denied_path.replace("callable-example", "other-shop"),
                                        json={**first_request, "shop_id": "other-shop"},
                                        headers=headers,
                                    )
                                ).status_code,
                                403,
                            )
                            invalid = {
                                **first_request,
                                "request_key": "bad-business",
                                "inputs": [
                                    {"name": "document", "value": "drawing-1"},
                                    {"name": "engagement", "value": "production"},
                                    common[0],
                                ],
                            }
                            self.assertEqual(
                                (
                                    await post(denied_path, json=invalid, headers=headers)
                                ).status_code,
                                409,
                            )
                            with repository.connection() as db:
                                self.assertEqual(
                                    db.execute(
                                        "SELECT COUNT(*) FROM simulation_effects"
                                    ).fetchone()[0],
                                    4,
                                )
            finally:
                await run(*cli, "down")
                volumes = (
                    await run(
                        "docker",
                        "volume",
                        "ls",
                        "-q",
                        "--filter",
                        f"label=com.docker.compose.project={name}",
                    )
                ).split()
                if volumes:
                    await run("docker", "volume", "rm", *volumes)
