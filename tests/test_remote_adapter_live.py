"""Keycloak/MCP authoring, real OpenBao ACLs, TLS receiver, and a contained executor."""

import asyncio
import http.client
import json
import os
import secrets
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from program_support import agent_call
from secrets_support import script as secrets_stack
from tls_support import ROOT, stack

from chipload.execution import ExecutionService
from chipload.execution_model import AdapterBinding
from chipload.lifecycle import Repository
from chipload.remote_adapter import HttpAdapterConfig
from examples.service_shop import project


def write(path, value):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.touch(mode=0o600)
    path.write_bytes(value if isinstance(value, bytes) else value.encode())


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@unittest.skipUnless(
    os.environ.get("CHIPLOAD_ADAPTER_TESTS") == "1",
    "requires built worker/runtime/TLS images and OpenBao",
)
class RemoteAdapterLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_scoped_secrets_receipts_restart_and_network_containment(self):
        bao_image = secrets_stack.image_environment.resolve_image(
            secrets_stack.IMAGE, secrets_stack.IMAGE_SHA256
        )
        with tempfile.TemporaryDirectory(prefix="chipload-adapter-") as temporary:
            root = Path(temporary)
            state, tls = root / "state", root / "adapter-tls"
            name = "chipload-adapter-" + uuid.uuid4().hex[:12]
            network, executor_name = name + "-network", name + "-executor"
            bao_name, receiver_name = name + "-bao", name + "-receiver"
            cli = [sys.executable, str(ROOT / "scripts/tls-stack"), "--state", str(state)]
            stack.local_certificates(tls, {"api": "receiver", "identity": "bao"})
            bao_address = "127.0.0.1"
            bao_context = ssl.create_default_context(cafile=tls / "identity-ca.pem")

            async def run(*args, timeout=240):
                result = await asyncio.to_thread(
                    subprocess.run, list(args), capture_output=True, text=True, timeout=timeout
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()

            def bao_request(method, path, body=None, token=None):
                class Loopback(http.client.HTTPSConnection):
                    def connect(self):
                        self.sock = bao_context.wrap_socket(
                            socket.create_connection((bao_address, self.port), timeout=3),
                            server_hostname=self.host,
                        )
                        self.sock.settimeout(30)

                connection = Loopback("bao", 8200, timeout=3, context=bao_context)
                try:
                    connection.request(
                        method,
                        "/v1/" + path,
                        body=json.dumps(body).encode() if body is not None else None,
                        headers={
                            "Content-Type": "application/json",
                            **({"X-Vault-Token": token} if token else {}),
                        },
                    )
                    response = connection.getresponse()
                    data = response.read(128 * 1024)
                    return response.status, json.loads(data) if data else None
                finally:
                    connection.close()

            async def bao(method, path, body=None, token=None, expected=200):
                status, result = await asyncio.to_thread(bao_request, method, path, body, token)
                self.assertEqual(status, expected)
                return result

            executor_image = (ROOT / ".local/executor-image").read_text().strip()
            bao_data, receiver_data, credentials = (
                root / "bao",
                root / "receiver",
                root / "credentials",
            )
            for directory in (bao_data, receiver_data, credentials):
                directory.mkdir(mode=0o700)
            bearer = secrets.token_urlsafe(32)
            control = receiver_data / "control.json"
            write(control, json.dumps({"token": bearer, "behavior": "normal"}))
            write(
                root / "bao.hcl",
                """
disable_mlock = true
ui = false
storage "raft" {
  path = "/bao/data/store"
  node_id = "adapter-test"
}
listener "tcp" {
  address = "0.0.0.0:8200"
  tls_cert_file = "/tls/bao.crt"
  tls_key_file = "/tls/bao.key"
}
api_addr = "https://bao:8200"
cluster_addr = "https://bao:8201"
audit "file" "file" {
  options { file_path = "/bao/data/audit.log" }
}
""",
            )
            base_docker = [
                "docker",
                "run",
                "--detach",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "64",
                "--memory",
                "512m",
                "--memory-swap",
                "512m",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=16m",
                "--network",
                network,
            ]
            executor_cli = None
            await run(
                *cli,
                "init",
                "--local",
                "--name",
                name,
                "--shop",
                "service-example",
                "--port",
                str(port()),
                "--admin-port",
                str(port()),
            )
            await run("docker", "network", "create", "--internal", network)
            try:
                await run(
                    *base_docker,
                    "--name",
                    bao_name,
                    "--network-alias",
                    "bao",
                    "--env",
                    "SKIP_CHOWN=true",
                    "--mount",
                    f"type=bind,src={root / 'bao.hcl'},dst=/openbao/config/adapter.hcl,readonly",
                    "--mount",
                    f"type=bind,src={bao_data},dst=/bao/data",
                    "--mount",
                    f"type=bind,src={tls / 'certificates/identity.crt'},dst=/tls/bao.crt,readonly",
                    "--mount",
                    f"type=bind,src={tls / 'certificates/identity.key'},dst=/tls/bao.key,readonly",
                    bao_image,
                    "server",
                )
                # Internal bridges do not publish ports. The Linux operator host can
                # administer the fixture at its bridge IP while still verifying TLS name.
                bao_address = json.loads(await run("docker", "inspect", bao_name))[0][
                    "NetworkSettings"
                ]["Networks"][network]["IPAddress"]
                readiness = "no response"
                for _ in range(100):
                    try:
                        status, _ = await asyncio.to_thread(bao_request, "GET", "sys/health")
                        if status == 501:
                            break
                        readiness = f"HTTP {status}"
                    except (OSError, http.client.HTTPException) as error:
                        readiness = str(error)
                    await asyncio.sleep(0.2)
                else:
                    self.fail(
                        f"OpenBao did not start ({readiness}): "
                        + await run("docker", "logs", bao_name)
                    )
                initialized = await bao(
                    "PUT", "sys/init", {"secret_shares": 1, "secret_threshold": 1}
                )
                await bao("PUT", "sys/unseal", {"key": initialized["keys_base64"][0]})
                root_token = initialized["root_token"]
                # Unsealing can return before the active node finishes startup.
                # Probe this read-only endpoint before issuing any configuration
                # writes. Authentication errors and persistent failures still fail.
                deadline = time.monotonic() + 20
                while True:
                    status, audit = await asyncio.to_thread(
                        bao_request, "GET", "sys/audit", None, root_token
                    )
                    if status == 200:
                        break
                    self.assertIn(status, (500, 503), "OpenBao audit readiness was rejected")
                    self.assertLess(time.monotonic(), deadline, "OpenBao audit API did not become ready")
                    await asyncio.sleep(0.2)
                self.assertEqual(audit["data"]["file/"]["type"], "file")
                await bao(
                    "POST",
                    "sys/mounts/chipload",
                    {"type": "kv", "options": {"version": "2"}},
                    root_token,
                    204,
                )
                await bao("POST", "sys/auth/approle", {"type": "approle"}, root_token, 204)
                secret_path = "chipload/data/shops/service-example/evidence"
                policy = f'path "{secret_path}" {{ capabilities = ["read"] }}\npath "auth/token/revoke-self" {{ capabilities = ["update"] }}'
                await bao(
                    "PUT", "sys/policies/acl/evidence-adapter", {"policy": policy}, root_token, 204
                )
                await bao(
                    "POST",
                    "auth/approle/role/evidence-adapter",
                    {
                        "token_policies": ["evidence-adapter"],
                        "token_no_default_policy": True,
                        "token_ttl": "60s",
                        "token_max_ttl": "60s",
                        "secret_id_ttl": "20m",
                    },
                    root_token,
                    204,
                )
                await bao("POST", secret_path, {"data": {"token": bearer}}, root_token)
                await bao(
                    "POST",
                    "chipload/data/shops/other-shop/private",
                    {"data": {"token": "other-scope-fixture"}},
                    root_token,
                )
                role = await bao(
                    "GET", "auth/approle/role/evidence-adapter/role-id", token=root_token
                )
                secret = await bao(
                    "POST", "auth/approle/role/evidence-adapter/secret-id", {}, root_token
                )
                login = {
                    "role_id": role["data"]["role_id"],
                    "secret_id": secret["data"]["secret_id"],
                }
                scoped = (await bao("POST", "auth/approle/login", login))["auth"]["client_token"]
                for method, path, body in (
                    ("GET", "chipload/data/shops/other-shop/private", None),
                    ("POST", secret_path, {"data": {"token": "must-not-write"}}),
                    ("LIST", "chipload/metadata/shops", None),
                    ("GET", "sys/policies/acl/evidence-adapter", None),
                ):
                    await bao(method, path, body, scoped, 403)
                await bao("POST", "auth/token/revoke-self", token=scoped, expected=204)
                write(credentials / "role-id", login["role_id"])
                write(credentials / "secret-id", login["secret_id"])
                write(credentials / "ca.pem", (tls / "identity-ca.pem").read_bytes())
                await run(
                    *base_docker,
                    "--name",
                    receiver_name,
                    "--network-alias",
                    "receiver",
                    "--entrypoint",
                    "python",
                    "--mount",
                    f"type=bind,src={ROOT / 'tests'},dst=/fixture,readonly",
                    "--mount",
                    f"type=bind,src={receiver_data},dst=/receiver",
                    "--mount",
                    f"type=bind,src={tls / 'certificates/api.crt'},dst=/tls/receiver.crt,readonly",
                    "--mount",
                    f"type=bind,src={tls / 'certificates/api.key'},dst=/tls/receiver.key,readonly",
                    executor_image,
                    "-I",
                    "-B",
                    "/fixture/serve_receiver.py",
                )
                config = HttpAdapterConfig(
                    id="evidence",
                    receiver_id="evidence-service",
                    origin="https://receiver:8443",
                    ca_file="ca.pem",
                    contracts=[
                        {
                            "shop_id": "service-example",
                            "operation_id": operation,
                            "command": command,
                            "effect": effect,
                        }
                        for operation, command, effect in (
                            ("read-evidence-status", "read-status", "read"),
                            ("request-evidence-review", "request-review", "write"),
                        )
                    ],
                    secrets={
                        "origin": "https://bao:8200",
                        "ca_file": "ca.pem",
                        "kv_mount": "chipload",
                        "secret_path": "shops/service-example/evidence",
                        "policy": "evidence-adapter",
                    },
                    timeout_seconds=5,
                )
                adapter_file = root / "adapter.json"
                write(adapter_file, config.model_dump_json())
                await run(*cli, "up")
                connection = json.loads((state / "client.json").read_bytes())
                base = connection["resource_url"][:-4]
                context = ssl.create_default_context(cafile=connection["ca_file"])
                async with httpx.AsyncClient(verify=context, trust_env=False, timeout=20) as rest:
                    auth = await rest.post(
                        connection["issuer"] + "/protocol/openid-connect/token",
                        data={
                            "grant_type": "client_credentials",
                            "client_id": connection["client_id"],
                            "client_secret": Path(connection["client_secret_file"]).read_text(),
                        },
                    )
                    self.assertEqual(auth.status_code, 200)
                    headers = {"Authorization": "Bearer " + auth.json()["access_token"]}
                    async with httpx2.AsyncClient(
                        verify=context, headers=headers, trust_env=False
                    ) as transport:
                        async with Client(
                            streamable_http_client(
                                connection["resource_url"], http_client=transport
                            )
                        ) as agent:

                            async def call(tool, body):
                                for _ in range(6):
                                    response = await agent_call(agent, tool, body)
                                    value = response.structured_content or {}
                                    if response.is_error and value.get("status") == 429:
                                        await asyncio.sleep(min(10, value["error"]["retry_after"]))
                                        continue
                                    self.assertFalse(response.is_error, response)
                                    return value
                                self.fail("request admission did not recover")

                            saved = await call(
                                "source_save",
                                {
                                    "project": project().model_dump(mode="json"),
                                    "expected_head": None,
                                },
                            )
                            build = await call(
                                "build_request",
                                {
                                    "shop_id": "service-example",
                                    "revision": saved["revision"],
                                    "request_key": "build",
                                },
                            )
                            for _ in range(150):
                                built = await call(
                                    "build_inspect",
                                    {"shop_id": "service-example", "build_id": build["id"]},
                                )
                                if built["state"] in {"succeeded", "failed"}:
                                    break
                                await asyncio.sleep(0.3)
                            self.assertEqual(built["state"], "succeeded", built["report"])
                            await call(
                                "publication_change",
                                {
                                    "shop_id": "service-example",
                                    "action": "publish",
                                    "build_id": build["id"],
                                    "expected_generation": 0,
                                },
                            )
                            repository = Repository(state / "data/chipload.db")
                            for contract in config.contracts:
                                ExecutionService(repository).bind(
                                    AdapterBinding(
                                        shop_id=contract.shop_id,
                                        operation_id=contract.operation_id,
                                        model_sha256=built["report"]["artifact"]["model_sha256"],
                                        adapter_id=config.id,
                                        adapter_sha256=config.fingerprint,
                                        mode="live",
                                    ),
                                    expected_generation=0,
                                )
                            executor_cli = [
                                sys.executable,
                                str(ROOT / "scripts/executor"),
                                "--settings",
                                str(state / "runtime.json"),
                                "--data",
                                str(state / "data"),
                                "--adapter",
                                str(adapter_file),
                                "--credentials",
                                str(credentials),
                                "--network",
                                network,
                                "--name",
                                executor_name,
                            ]
                            await run(*executor_cli, "up")

                            def request(key, operation="request-evidence-review"):
                                return {
                                    "shop_id": "service-example",
                                    "operation_id": operation,
                                    "expected_generation": 1,
                                    "request_key": key,
                                    "mode": "live",
                                    "inputs": [
                                        {"name": "record-reference", "value": "record-1"},
                                        {"name": "purpose", "value": "prototype-verification"},
                                    ],
                                }

                            async def read(job, desired):
                                path = (
                                    base
                                    + f"/v1/shops/service-example/operations/{job['operation_id']}/executions/{job['id']}"
                                )
                                for _ in range(150):
                                    response = await rest.get(path, headers=headers)
                                    self.assertEqual(response.status_code, 200)
                                    value = response.json()
                                    if value["state"] == desired:
                                        return value
                                    await asyncio.sleep(0.2)
                                self.fail(
                                    f"execution did not reach {desired}; state={value['state']}"
                                )

                            first = await call("operation_submit", request("first"))
                            completed = await read(first, "succeeded")
                            self.assertEqual(completed["mode"], "live")
                            self.assertEqual(
                                (await call("operation_submit", request("first")))["id"],
                                first["id"],
                            )
                            read_job = await call(
                                "operation_submit", request("read", "read-evidence-status")
                            )
                            await read(read_job, "succeeded")
                            write(
                                control,
                                json.dumps({"token": bearer, "behavior": "lost-and-missing"}),
                            )
                            lost = await call("operation_submit", request("lost"))
                            await read(lost, "indeterminate")
                            await run(*executor_cli, "down")
                            await run("docker", "restart", receiver_name)
                            write(control, json.dumps({"token": bearer, "behavior": "normal"}))
                            await run(*executor_cli, "up")
                            await read(lost, "succeeded")
                            with sqlite3.connect(receiver_data / "receipts.db") as db:
                                self.assertEqual(
                                    db.execute("SELECT COUNT(*) FROM effects").fetchone()[0], 2
                                )
                            # Secret rotation is consumed without changing the binding or baking a token into a container.
                            bearer = secrets.token_urlsafe(32)
                            await bao("POST", secret_path, {"data": {"token": bearer}}, root_token)
                            write(control, json.dumps({"token": bearer, "behavior": "normal"}))
                            rotated = await call(
                                "operation_submit", request("rotated", "read-evidence-status")
                            )
                            await read(rotated, "succeeded")
                            # The mounted credential directory sees atomic AppRole rotation.
                            replacement = await bao(
                                "POST",
                                "auth/approle/role/evidence-adapter/secret-id",
                                {},
                                root_token,
                            )
                            write(credentials / "secret-id.next", replacement["data"]["secret_id"])
                            (credentials / "secret-id.next").replace(credentials / "secret-id")
                            await bao(
                                "POST",
                                "auth/approle/role/evidence-adapter/secret-id-accessor/destroy",
                                {"secret_id_accessor": secret["data"]["secret_id_accessor"]},
                                root_token,
                                204,
                            )
                            await bao("POST", "auth/approle/login", login, expected=400)
                            renewed = await call(
                                "operation_submit", request("renewed-role", "read-evidence-status")
                            )
                            await read(renewed, "succeeded")
                            # Sealing the secret store must prevent any new receiver command.
                            await bao("PUT", "sys/seal", token=root_token, expected=204)
                            sealed = await call("operation_submit", request("sealed"))
                            result = await read(sealed, "rejected")
                            self.assertEqual(
                                result["result"]["code"], "adapter-credential-unavailable"
                            )
                            await bao("PUT", "sys/unseal", {"key": initialized["keys_base64"][0]})
                            # A stalled receiver cannot prevent graceful executor shutdown.
                            write(control, json.dumps({"token": bearer, "behavior": "stall"}))
                            # Observe the short running interval immediately;
                            # a completed Python run also waits for container cleanup.
                            admitted = await rest.post(
                                base
                                + "/v1/shops/service-example/operations/request-evidence-review/executions",
                                json=request("stalled"),
                                headers=headers,
                            )
                            self.assertEqual(admitted.status_code, 202, admitted.text)
                            stalled = admitted.json()
                            await read(stalled, "running")
                            start = time.monotonic()
                            await run(*executor_cli, "down")
                            self.assertLess(time.monotonic() - start, 8)
                            await read(stalled, "indeterminate")
                            write(control, json.dumps({"token": bearer, "behavior": "normal"}))
                            await call(
                                "operation_execution_cancel",
                                {
                                    "shop_id": "service-example",
                                    "operation_id": stalled["operation_id"],
                                    "execution_id": stalled["id"],
                                },
                            )
                            await run(*executor_cli, "up")
                            await read(stalled, "cancelled")
                            identifier = await run(
                                "docker",
                                "ps",
                                "-q",
                                "--filter",
                                f"label=com.docker.compose.project={executor_name}",
                            )
                            inspected = json.loads(await run("docker", "inspect", identifier))[0]
                            self.assertEqual(
                                inspected["Config"]["User"], f"{os.getuid()}:{os.getgid()}"
                            )
                            self.assertTrue(inspected["HostConfig"]["ReadonlyRootfs"])
                            self.assertEqual(inspected["HostConfig"]["CapDrop"], ["ALL"])
                            self.assertEqual(
                                set(inspected["NetworkSettings"]["Networks"]), {network}
                            )
                            self.assertFalse(inspected["NetworkSettings"]["Ports"])
                            self.assertTrue(
                                all(
                                    m["Destination"] != "/var/run/docker.sock"
                                    for m in inspected["Mounts"]
                                )
                            )
                            mounts = {m["Destination"]: m for m in inspected["Mounts"]}
                            self.assertFalse(mounts["/run/chipload/credentials"]["RW"])
                            logs = await run("docker", "logs", identifier)
                            self.assertNotIn(bearer, logs)
                            self.assertNotIn(login["secret_id"], logs)
                            api = await run(
                                "docker",
                                "ps",
                                "-q",
                                "--filter",
                                f"label=com.docker.compose.project={name}",
                                "--filter",
                                "label=com.docker.compose.service=api",
                            )
                            private = json.loads(await run("docker", "inspect", api))[0]
                            self.assertNotIn(network, private["NetworkSettings"]["Networks"])
                            self.assertFalse(
                                any("credentials" in m["Destination"] for m in private["Mounts"])
                            )
                            with repository.connection() as db:
                                stored = b"".join(
                                    bytes(row[0])
                                    for row in db.execute("SELECT request FROM executions")
                                )
                                stored += b"".join(
                                    bytes(row[0])
                                    for row in db.execute(
                                        "SELECT result FROM executions WHERE result IS NOT NULL"
                                    )
                                )
                            self.assertNotIn(bearer.encode(), stored)
                            self.assertNotIn(login["secret_id"].encode(), stored)
            finally:
                if executor_cli:
                    await run(*executor_cli, "down")
                await run(*cli, "down")
                for container in (receiver_name, bao_name):
                    exists = await run("docker", "ps", "-aq", "--filter", f"name=^/{container}$")
                    if exists:
                        await run("docker", "rm", "-f", exists)
                await run("docker", "network", "rm", network)
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
