"""External network clients, explicit TLS binding and actual peer accounting."""

import hashlib
import ipaddress
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from credentials_support import credentials
from tls_support import ROOT, stack

from chipload.client import Client, route
from chipload.lifecycle import Repository
from chipload.quotas import QuotaPolicy
from examples.export_project import project

PROBE = r"""
import http.client, json, socket, ssl, sys
v = json.load(sys.stdin)
context = ssl.create_default_context(cadata=v["ca"])
def request(path, *, host=None, headers=None, body=None, trust=context):
    host = host or v["api"]
    connection = http.client.HTTPSConnection(host, v["port"], timeout=5, context=trust)
    raw = socket.create_connection((v["address"], v["port"]), timeout=5)
    try:
        connection.sock = trust.wrap_socket(raw, server_hostname=host)
        connection.request("POST" if body else "GET", path,
            body=json.dumps(body) if body else None, headers=headers or {})
        response = connection.getresponse()
        content = response.read(1024*1024+1)
        assert len(content) <= 1024*1024
        return response.status, content
    finally:
        connection.close()
        raw.close()
def reachable(address, port):
    try:
        with socket.create_connection((address, port), timeout=1):
            return True
    except OSError:
        return False
if v["mode"] == "limits":
    statuses=[]
    for i in range(v["count"]):
        statuses.append(request("/v1/shops/thermal-example/catalog", headers={
            "X-Forwarded-For": "198.51.100."+str(i+1),
            "X-Real-IP": "203.0.113.99", "Forwarded": "for=203.0.113.88"})[0])
    print(json.dumps(statuses))
else:
    result={}
    for name, path in (("health", "/healthz"),
                       ("catalog", "/v1/shops/thermal-example/catalog"),
                       ("private", "/v1/shops/thermal-example/source")):
        result[name]=request(path)[0]
    result["metadata"]=request("/realms/chipload/.well-known/openid-configuration",host=v["identity"])[0]
    result["admin_path"]=request("/admin/realms",host=v["identity"])[0]
    result["master"]=request("/realms/master/.well-known/openid-configuration",host=v["identity"])[0]
    result["host_mismatch"]=request("/healthz",headers={"Host":v["identity"]+":"+str(v["port"])})[0]
    status, body=request("/mcp",headers={"Accept":"application/json, text/event-stream", "Content-Type":"application/json"},body={
        "jsonrpc":"2.0","id":1,"method":"tools/call","params":{
            "name":"discover_shop","arguments":{"request":{"shop_id":"thermal-example"}},
            "_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28", "io.modelcontextprotocol/clientCapabilities":{}}}})
    result["mcp"]=status
    result["mcp_error"]=json.loads(body).get("result",{}).get("isError", True)
    result["admin_reachable"]=reachable(v["address"],v["admin_port"])
    result["api_reachable"]=reachable(v["api_address"],8088)
    try:
        request("/healthz",trust=ssl.create_default_context())
        result["untrusted_ca"]=True
    except ssl.SSLCertVerificationError:
        result["untrusted_ca"]=False
    print(json.dumps(result))
"""


@unittest.skipUnless(
    os.environ.get("CHIPLOAD_TLS_TESTS") == "1", "requires TLS/runtime images and Docker"
)
class EdgeLiveTests(unittest.TestCase):
    def test_explicit_listener_preserves_private_admin_and_distinct_external_peer_budgets(self):
        with tempfile.TemporaryDirectory(prefix="chipload-edge-test-") as temporary:
            root = Path(temporary)
            state = root / "state"
            name = "chipload-edge-test-" + uuid.uuid4().hex[:10]
            network_name = name + "-clients"
            network = ipaddress.ip_network(stack.choose_subnet())
            address = str(network[1])
            hosts = {"api": "shop.example.test", "identity": "identity.example.test"}
            stack.local_certificates(root / "certificates", hosts)
            cli = [sys.executable, str(ROOT / "scripts/tls-stack"), "--state", str(state)]

            def run(*arguments, input=None):
                result = subprocess.run(
                    arguments, input=input, capture_output=True, text=True, timeout=300
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()

            # Keep the two client source addresses distinct at the published port.
            # Default bridge masquerading would correctly give NAT peers one budget.
            run(
                "docker",
                "network",
                "create",
                "--subnet",
                str(network),
                "--opt",
                "com.docker.network.bridge.gateway_mode_ipv4=routed",
                network_name,
            )
            listeners = [socket.socket(), socket.socket()]
            try:
                for listener, binding in zip(listeners, (address, "127.0.0.1"), strict=True):
                    listener.bind((binding, 0))
                port, admin_port = (listener.getsockname()[1] for listener in listeners)
            finally:
                for listener in listeners:
                    listener.close()
            try:
                run(
                    *cli,
                    "init",
                    "--api-host",
                    hosts["api"],
                    "--identity-host",
                    hosts["identity"],
                    "--port",
                    str(port),
                    "--admin-port",
                    str(admin_port),
                    "--listen-address",
                    address,
                    "--name",
                    name,
                    "--shop",
                    "thermal-example",
                    "--certificates",
                    str(root / "certificates/certificates"),
                    "--ca-bundle",
                    str(root / "certificates/identity-ca.pem"),
                )
                run(*cli, "up")
                values = stack.configuration(json.loads((state / "deployment.json").read_bytes()))
                status, auth = credentials.agent_token(
                    state, values, (state / "secrets/agent-secret").read_text()
                )
                self.assertEqual(status, 200)
                token = auth["access_token"]

                def call(operation, request):
                    method, path, data = route(operation, request)
                    status, result = credentials.request(
                        state, values, path, method=method, data=data, token=token, api=True
                    )
                    self.assertIn(status, (200, 202))
                    return result

                shop = Client(call).shop("thermal-example")
                source = shop.save(project(), expected_head=None)
                build = shop.build(source["revision"], request_key="external-build")
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    build = shop.build_result(build["id"])
                    if build["state"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.4)
                self.assertEqual(build["state"], "succeeded")
                shop.publish(build["id"], expected_generation=0)
                service_ids = run(
                    "docker", "ps", "-q", "--filter", "label=com.docker.compose.project=" + name
                ).split()
                containers = json.loads(run("docker", "inspect", *service_ids))
                services = {
                    item["Config"]["Labels"]["com.docker.compose.service"]: item
                    for item in containers
                }
                self.assertEqual(
                    services["gateway"]["HostConfig"]["PortBindings"],
                    {
                        "8443/tcp": [{"HostIp": address, "HostPort": str(port)}],
                        "8444/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(admin_port)}],
                    },
                )
                for role in ("api", "broker", "keycloak", "keycloak-db"):
                    self.assertFalse(services[role]["HostConfig"]["PortBindings"])
                payload = {
                    **hosts,
                    "address": address,
                    "port": port,
                    "admin_port": admin_port,
                    "ca": (state / "identity-ca.pem").read_text(),
                    "api_address": str(ipaddress.ip_network(values["subnet"])[3]),
                }

                def external(index, **options):
                    return json.loads(
                        run(
                            "docker",
                            "run",
                            "--rm",
                            "--interactive",
                            "--name",
                            name + "-client-" + str(index),
                            "--network",
                            network_name,
                            "--ip",
                            str(network[index]),
                            "--read-only",
                            "--cap-drop=ALL",
                            "--security-opt=no-new-privileges:true",
                            "--memory=128m",
                            "--pids-limit=32",
                            "--entrypoint=python",
                            stack.runtime.image("api"),
                            "-c",
                            PROBE,
                            input=json.dumps({**payload, **options}),
                        )
                    )

                self.assertEqual(
                    external(2, mode="discovery"),
                    {
                        "health": 200,
                        "catalog": 200,
                        "private": 401,
                        "metadata": 200,
                        "admin_path": 404,
                        "master": 404,
                        "host_mismatch": 421,
                        "mcp": 200,
                        "mcp_error": False,
                        "admin_reachable": False,
                        "api_reachable": False,
                        "untrusted_ca": False,
                    },
                )
                repository = Repository(state / "data/chipload.db")
                quotas = repository.read_quotas()
                limited = QuotaPolicy.model_validate(quotas["policy"]).model_dump(mode="json")
                limited["requests"]["peer"] = {"per_minute": 1, "burst": 4}
                repository.set_quotas(
                    QuotaPolicy.model_validate(limited), expected_generation=quotas["generation"]
                )
                try:
                    self.assertEqual(external(3, mode="limits", count=8), [200] * 4 + [429] * 4)
                    self.assertEqual(external(4, mode="limits", count=1), [200])
                    with repository.admission.connection() as db:
                        keys = {
                            row[0]
                            for row in db.execute(
                                "SELECT subject FROM request_buckets WHERE scope='peer'"
                            )
                        }
                    # The earlier discovery client's full bucket may already
                    # have been reclaimed; these two clients retain spent credit.
                    for index in (3, 4):
                        self.assertIn(hashlib.sha256(str(network[index]).encode()).digest(), keys)
                    self.assertNotIn(hashlib.sha256(b"198.51.100.1").digest(), keys)
                finally:
                    current = repository.read_quotas()
                    repository.set_quotas(
                        QuotaPolicy.model_validate(quotas["policy"]),
                        expected_generation=current["generation"],
                    )
                # Administrative helpers still reach the separate loopback listener.
                bearer = credentials.operator_token(
                    state, values, "admin", state / "secrets/admin-password"
                )
                self.assertEqual(
                    credentials.request(
                        state, values, "/admin/realms/chipload", token=bearer, admin=True
                    )[0],
                    200,
                )
            finally:
                for index in (2, 3, 4):
                    subprocess.run(
                        ["docker", "rm", "-f", name + "-client-" + str(index)], capture_output=True
                    )
                if state.exists():
                    run(*cli, "down")
                volumes = run(
                    "docker",
                    "volume",
                    "ls",
                    "-q",
                    "--filter",
                    "label=com.docker.compose.project=" + name,
                ).split()
                if volumes:
                    run("docker", "volume", "rm", *volumes)
                run("docker", "network", "rm", network_name)
