"""Real Keycloak, verified TLS, container isolation, and the shop lifecycle."""

import asyncio
import http.client
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
from urllib.parse import urlencode

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from program_support import AdmissionRetryTransport, agent_call
from tls_support import ROOT, stack

from chipload.lifecycle import Repository
from chipload.quotas import QuotaPolicy
from examples.export_project import project


@unittest.skipUnless(
    os.environ.get("CHIPLOAD_TLS_TESTS") == "1", "requires worker, runtime, and TLS images"
)
class TLSLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_supplied_certificates_rotate_without_changing_identity_or_grants(self):
        with tempfile.TemporaryDirectory(prefix="chipload-tls-rotation-") as temporary:
            root = Path(temporary)
            state = root / "state"
            hosts = {"api": "shop.example.test", "identity": "identity.example.test"}
            old = root / "old"
            stack.local_certificates(old, hosts)
            listeners = [socket.socket(), socket.socket()]
            for listener in listeners:
                listener.bind(("127.0.0.1", 0))
            port, admin_port = (listener.getsockname()[1] for listener in listeners)
            for listener in listeners:
                listener.close()
            name = "chipload-tls-rotation-" + uuid.uuid4().hex[:12]
            cli = [sys.executable, str(ROOT / "scripts/tls-stack"), "--state", str(state)]

            async def run(*arguments, expected=0):
                result = await asyncio.to_thread(
                    subprocess.run, list(arguments), capture_output=True, text=True, timeout=240
                )
                self.assertEqual(result.returncode, expected, result.stderr)
                return result.stdout.strip()

            await run(
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
                "--name",
                name,
                "--certificates",
                str(old / "certificates"),
                "--ca-bundle",
                str(old / "identity-ca.pem"),
            )

            def verify_identity(ca_file):
                context = ssl.create_default_context(cafile=ca_file)

                class LoopbackConnection(http.client.HTTPSConnection):
                    def connect(self):
                        self.sock = context.wrap_socket(
                            socket.create_connection(("127.0.0.1", self.port), timeout=10),
                            server_hostname=self.host,
                        )

                identity = LoopbackConnection(hosts["identity"], port, timeout=10, context=context)
                try:
                    identity.request(
                        "POST",
                        "/realms/chipload/protocol/openid-connect/token",
                        body=urlencode(
                            {
                                "grant_type": "client_credentials",
                                "client_id": "chipload-agent",
                                "client_secret": (state / "secrets/agent-secret").read_text(),
                            }
                        ),
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
                    response = identity.getresponse()
                    self.assertEqual(response.status, 200)
                    token = json.loads(response.read())["access_token"]
                finally:
                    identity.close()
                api = LoopbackConnection(hosts["api"], port, timeout=10, context=context)
                try:
                    api.request("GET", "/v1/identity", headers={"Authorization": "Bearer " + token})
                    response = api.getresponse()
                    self.assertEqual(response.status, 200)
                    return json.loads(response.read())
                finally:
                    api.close()

            try:
                await run(*cli, "up")
                original = await asyncio.to_thread(verify_identity, old / "identity-ca.pem")
                configuration = (state / "runtime.json").read_bytes()
                realm = (state / "realm.json").read_bytes()
                renewed = root / "renewed"
                stack.local_certificates(renewed, hosts)
                certificate_cli = [
                    sys.executable,
                    str(ROOT / "scripts/tls-certificates"),
                    "--state",
                    str(state),
                ]
                status = json.loads(await run(*certificate_cli, "status", expected=2))
                self.assertTrue(status["valid"])
                self.assertTrue(status["renewal_due"])
                # Exit the actual operator process after replacing only one file.
                # A different process must recover from the durable journal.
                crash = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from certificates_support import certificates as script
replace = script.replace_file
def interrupted(*args):
    replace(*args)
    os._exit(99)
script.replace_file = interrupted
script.install(Path(sys.argv[2]), certificates=Path(sys.argv[3]) / 'certificates', ca_bundle=Path(sys.argv[3]) / 'identity-ca.pem')
"""
                await run(
                    sys.executable,
                    "-c",
                    crash,
                    str(ROOT / "tests"),
                    str(state),
                    str(renewed),
                    expected=99,
                )
                self.assertTrue((state / ".certificates-incomplete.json").exists())
                await run(*cli, "up", expected=1)
                await run(*certificate_cli, "resume")
                after = await asyncio.to_thread(verify_identity, renewed / "identity-ca.pem")
                self.assertEqual(after, original)
                self.assertTrue((state / "runtime.json").read_bytes() == configuration)
                self.assertTrue((state / "realm.json").read_bytes() == realm)
                with self.assertRaises(ssl.SSLCertVerificationError):
                    await asyncio.to_thread(
                        stack.probe, hosts["api"], port, "/healthz", old / "identity-ca.pem"
                    )
                receipt = json.loads((state / "certificates.json").read_bytes())
                self.assertTrue(receipt["live_verified"])
                await asyncio.to_thread(
                    stack.probe,
                    hosts["api"],
                    port,
                    "/healthz",
                    renewed / "identity-ca.pem",
                    certificate_sha256=receipt["leaves"]["api"]["sha256"],
                )
                with self.assertRaisesRegex(ValueError, "installed certificate"):
                    await asyncio.to_thread(
                        stack.probe,
                        hosts["api"],
                        port,
                        "/healthz",
                        renewed / "identity-ca.pem",
                        certificate_sha256="0" * 64,
                    )
                # Lose completion after real activation and stage removal. Resume
                # must work even though the staged files no longer exist.
                crash = crash.replace(
                    "replace = script.replace_file", "replace = script.remove_stage"
                ).replace("script.replace_file = interrupted", "script.remove_stage = interrupted")
                await run(
                    sys.executable,
                    "-c",
                    crash,
                    str(ROOT / "tests"),
                    str(state),
                    str(renewed),
                    expected=99,
                )
                self.assertEqual(
                    json.loads((state / ".certificates-incomplete.json").read_bytes())["phase"],
                    "verified",
                )
                await run(*cli, "down")
                await run(*certificate_cli, "resume")
                self.assertFalse((state / ".certificates-incomplete.json").exists())
                running = json.loads(
                    await run(
                        "docker",
                        "inspect",
                        *(
                            await run(
                                "docker",
                                "ps",
                                "-q",
                                "--filter",
                                f"label=com.docker.compose.project={name}",
                            )
                        ).split(),
                    )
                )
                self.assertEqual(
                    {
                        container["Config"]["Labels"]["com.docker.compose.service"]
                        for container in running
                    },
                    {"gateway", "api", "keycloak", "keycloak-db", "broker"},
                )
                self.assertEqual(
                    await asyncio.to_thread(verify_identity, renewed / "identity-ca.pem"), original
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

    async def test_verified_identity_agent_lifecycle_and_private_networks(self):
        with tempfile.TemporaryDirectory(prefix="chipload-tls-test-") as temporary:
            state = Path(temporary) / "state"
            listeners = [socket.socket(), socket.socket()]
            for listener in listeners:
                listener.bind(("127.0.0.1", 0))
            port, admin_port = (listener.getsockname()[1] for listener in listeners)
            for listener in listeners:
                listener.close()
            name = "chipload-tls-test-" + uuid.uuid4().hex[:12]
            cli = [sys.executable, str(ROOT / "scripts/tls-stack"), "--state", str(state)]

            async def run(*arguments, timeout=240):
                result = await asyncio.to_thread(
                    subprocess.run, list(arguments), capture_output=True, text=True, timeout=timeout
                )
                self.assertEqual(result.returncode, 0, result.stderr)
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
                "thermal-example",
            )
            try:
                await run(*cli, "up")
                connection = json.loads((state / "client.json").read_bytes())
                base = connection["resource_url"][:-4]
                context = ssl.create_default_context(cafile=connection["ca_file"])
                credentials = {
                    "grant_type": "client_credentials",
                    "client_id": connection["client_id"],
                    "client_secret": Path(connection["client_secret_file"]).read_text(),
                }
                raw_runtime = (state / "runtime.json").read_bytes()

                async def service_id(service):
                    return await run(
                        "docker",
                        "ps",
                        "-q",
                        "--filter",
                        f"label=com.docker.compose.project={name}",
                        "--filter",
                        f"label=com.docker.compose.service={service}",
                    )

                async with httpx.AsyncClient(verify=context, trust_env=False, timeout=20) as rest:

                    async def token():
                        response = await rest.post(
                            connection["issuer"] + "/protocol/openid-connect/token",
                            data=credentials,
                        )
                        self.assertEqual(response.status_code, 200)
                        return response.json()["access_token"]

                    async def ready():
                        for _ in range(100):
                            try:
                                if (await rest.get(base + "/healthz")).status_code == 200:
                                    return
                            except httpx.TransportError:
                                pass
                            await asyncio.sleep(0.1)
                        self.fail("API did not become ready")

                    bearer = await token()
                    headers = {"Authorization": "Bearer " + bearer}
                    principal = await rest.get(base + "/v1/identity", headers=headers)
                    self.assertEqual(principal.status_code, 200)
                    self.assertEqual(principal.json()["subject"], connection["subject"])
                    self.assertEqual(principal.json()["grants"][0]["shop_id"], "thermal-example")
                    metadata = (
                        await rest.get(base + "/.well-known/oauth-protected-resource/mcp")
                    ).json()
                    self.assertEqual(metadata["resource"], connection["resource_url"])
                    self.assertEqual(metadata["authorization_servers"], [connection["issuer"]])
                    self.assertEqual(
                        (await rest.get(base + "/v1/shops/thermal-example/source")).status_code, 401
                    )
                    self.assertEqual(
                        (
                            await rest.get(base + "/v1/shops/other-shop/source", headers=headers)
                        ).status_code,
                        403,
                    )
                    identity_base = connection["issuer"].removesuffix("/realms/chipload")
                    for path in (
                        "/admin/realms",
                        "/realms/master/.well-known/openid-configuration",
                        "/health/ready",
                        "/metrics",
                    ):
                        self.assertEqual((await rest.get(identity_base + path)).status_code, 404)
                    admin_base = f"https://identity.localhost:{admin_port}"
                    admin_discovery = await rest.get(
                        admin_base + "/realms/master/.well-known/openid-configuration"
                    )
                    self.assertEqual(admin_discovery.status_code, 200)
                    fronted = await rest.get(
                        base + "/healthz", headers={"Host": f"identity.localhost:{port}"}
                    )
                    self.assertEqual(fronted.status_code, 421)
                    too_large = await rest.post(
                        base + "/v1/shops/thermal-example/evaluate",
                        content=b"x" * (3 * 1024 * 1024 + 1),
                        headers={"Content-Type": "application/json"},
                    )
                    self.assertEqual(too_large.status_code, 413)
                    async with httpx.AsyncClient(trust_env=False, timeout=5) as untrusted:
                        with self.assertRaises(httpx.ConnectError):
                            await untrusted.get(base + "/healthz")

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
                                result = await agent_call(agent, tool, request)
                                self.assertFalse(result.is_error, result)
                                return result.structured_content

                            async def build(revision, key):
                                job = await call(
                                    "build_request",
                                    {
                                        "shop_id": "thermal-example",
                                        "revision": revision,
                                        "request_key": key,
                                    },
                                )
                                for _ in range(150):
                                    result = await call(
                                        "build_inspect",
                                        {"shop_id": "thermal-example", "build_id": job["id"]},
                                    )
                                    if result["state"] in {"failed", "succeeded"}:
                                        return result
                                    await asyncio.sleep(0.3)
                                self.fail("build did not finish")

                            source = project().model_dump(mode="json")
                            saved = await call(
                                "source_save", {"project": source, "expected_head": None}
                            )
                            first = await build(saved["revision"], "first")
                            self.assertEqual(first["state"], "succeeded", first["report"])
                            self.assertEqual(len(first["report"]["tests"]), 9)
                            await call(
                                "publication_change",
                                {
                                    "shop_id": "thermal-example",
                                    "action": "publish",
                                    "build_id": first["id"],
                                    "expected_generation": 0,
                                },
                            )
                            original = (
                                await rest.get(base + "/v1/shops/thermal-example/catalog")
                            ).json()
                            self.assertEqual(
                                await call("shop_catalog", {"shop_id": "thermal-example"}), original
                            )
                            source["files"][0]["content"] = source["files"][0]["content"].replace(
                                "Thermal Processing Example", "Updated through TLS"
                            )
                            saved = await call(
                                "source_save",
                                {"project": source, "expected_head": saved["revision"]},
                            )
                            second = await build(saved["revision"], "second")
                            self.assertEqual(second["state"], "succeeded", second["report"])
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
                                (await rest.get(base + "/v1/shops/thermal-example/catalog")).json()[
                                    "shop"
                                ]["name"],
                                "Updated through TLS",
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

                    api = await service_id("api")
                    invalid_ca = json.loads(raw_runtime)
                    invalid_ca["jwks_ca_file"] = "/etc/ssl/certs/ca-certificates.crt"
                    try:
                        (state / "runtime.json").write_text(json.dumps(invalid_ca))
                        await run("docker", "restart", api)
                        await ready()
                        denied = await rest.get(base + "/v1/identity", headers=headers)
                        self.assertEqual(denied.status_code, 503)
                        self.assertEqual(denied.json()["error"]["code"], "identity_unavailable")
                        self.assertEqual(
                            (await rest.get(base + "/v1/shops/thermal-example/catalog")).json(),
                            original,
                        )
                    finally:
                        (state / "runtime.json").write_bytes(raw_runtime)
                    await run(*cli, "down")
                    await run(*cli, "up")
                    bearer = await token()
                    headers = {"Authorization": "Bearer " + bearer}
                    self.assertEqual(
                        (await rest.get(base + "/v1/identity", headers=headers)).status_code, 200
                    )
                    self.assertEqual(
                        (await rest.get(base + "/v1/shops/thermal-example/catalog")).json(),
                        original,
                    )

                    names = [
                        await service_id(service)
                        for service in ("api", "broker", "keycloak", "keycloak-db", "gateway")
                    ]
                    inspections = json.loads(await run("docker", "inspect", *names))
                    for record in inspections:
                        self.assertTrue(record["HostConfig"]["ReadonlyRootfs"])
                        self.assertEqual(record["HostConfig"]["CapDrop"], ["ALL"])
                        self.assertIn("no-new-privileges:true", record["HostConfig"]["SecurityOpt"])
                    for record in inspections[:4]:
                        self.assertEqual(record["HostConfig"]["PortBindings"], {})
                    self.assertEqual(
                        {
                            value["HostIp"]
                            for bindings in inspections[4]["HostConfig"]["PortBindings"].values()
                            for value in bindings
                        },
                        {"127.0.0.1"},
                    )
                    self.assertEqual(
                        set(inspections[0]["NetworkSettings"]["Networks"]), {name + "_application"}
                    )
                    self.assertEqual(inspections[1]["HostConfig"]["NetworkMode"], "none")
                    networks = json.loads(
                        await run(
                            "docker",
                            "network",
                            "inspect",
                            name + "_application",
                            name + "_database",
                        )
                    )
                    self.assertTrue(all(network["Internal"] for network in networks))
                    self.assertEqual(
                        {m["Destination"] for m in inspections[0]["Mounts"]},
                        {"/data", "/etc/chipload/settings.json", "/etc/chipload/identity-ca.pem"},
                    )
                    await run(
                        "docker",
                        "exec",
                        names[0],
                        "python",
                        "-I",
                        "-c",
                        """
import socket
try:
    socket.create_connection(('1.1.1.1', 443), timeout=0.5)
except OSError:
    pass
else:
    raise AssertionError('API has unrestricted outbound network access')
""",
                    )
                    changed = await rest.put(
                        base + "/v1/shops/thermal-example/publication",
                        headers=headers,
                        json={
                            "shop_id": "thermal-example",
                            "action": "withdraw",
                            "expected_generation": 3,
                        },
                    )
                    self.assertEqual(changed.status_code, 200)
                    self.assertEqual(
                        (await rest.get(base + "/v1/shops/thermal-example/catalog")).status_code,
                        404,
                    )

                    repository = Repository(state / "data/chipload.db")
                    quota = repository.read_quotas()
                    quota["policy"]["requests"]["peer"] = {"per_minute": 1, "burst": 1}
                    repository.set_quotas(
                        QuotaPolicy.model_validate(quota["policy"]),
                        expected_generation=quota["generation"],
                    )
                    self.assertEqual((await rest.get(base + "/healthz")).status_code, 200)
                    spoofed = await rest.get(
                        base + "/healthz",
                        headers={
                            "X-Forwarded-For": "198.51.100.31",
                            "Forwarded": "for=198.51.100.32",
                            "X-Real-IP": "198.51.100.33",
                        },
                    )
                    self.assertEqual(spoofed.status_code, 429)
                    self.assertEqual(spoofed.json()["error"]["limit"], "peer")
                    logs = await run(*cli, "logs")
                    for secret in (
                        credentials["client_secret"],
                        (state / "secrets/admin-password").read_text(),
                        (state / "secrets/db-password").read_text(),
                        bearer,
                    ):
                        self.assertFalse(secret in logs, "credentials appeared in service logs")
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


if __name__ == "__main__":
    unittest.main()
