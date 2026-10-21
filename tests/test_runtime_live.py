"""Exercise the shipped API/broker Compose topology with real isolated workers."""

import asyncio
import json
import os
import socket
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from program_support import agent_call
from server_support import IdentityFixture

from examples.export_project import project

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(
    os.environ.get("CHIPLOAD_RUNTIME_TESTS") == "1", "requires runtime and worker images"
)
class RuntimeLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_container_authoring_isolation_restart_and_publication(self):
        with tempfile.TemporaryDirectory(prefix="chipload-runtime-test-") as temporary:
            directory = Path(temporary)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            base = f"http://127.0.0.1:{port}"
            fixture = IdentityFixture(directory, base=base)
            self.addCleanup(fixture.client.close)
            fixture.settings = fixture.settings.model_copy(
                update={
                    "database": Path("/data/chipload.db"),
                    "issuer": "http://127.0.0.1:8999/realms/chipload",
                    "jwks_url": "http://127.0.0.1:8999/jwks.json",
                }
            )
            config = directory / "runtime.json"
            config.write_text(fixture.settings.model_dump_json())
            config.chmod(0o600)
            name = "chipload-runtime-test-" + uuid.uuid4().hex[:12]
            command = [
                str(ROOT / "scripts/runtime"),
                "up",
                "--config",
                str(config),
                "--data",
                str(directory / "data"),
                "--port",
                str(port),
                "--name",
                name,
            ]

            async def run(*arguments, input=None, timeout=90):
                result = await asyncio.to_thread(
                    subprocess.run,
                    list(arguments),
                    input=input,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()

            async def container(service):
                return await run(
                    "docker",
                    "ps",
                    "--filter",
                    f"label=com.docker.compose.project={name}",
                    "--filter",
                    f"label=com.docker.compose.service={service}",
                    "--format",
                    "{{.ID}}",
                )

            async def identity_server(api):
                # Only public verification keys enter the API namespace. The test
                # signer stays in this process; production uses the configured Keycloak.
                await run(
                    "docker",
                    "exec",
                    "-i",
                    api,
                    "python",
                    "-I",
                    "-c",
                    "import pathlib,sys; pathlib.Path('/tmp/jwks.json').write_text(sys.stdin.read())",
                    input=json.dumps({"keys": fixture.keys}),
                )
                await run(
                    "docker",
                    "exec",
                    "-d",
                    api,
                    "python",
                    "-I",
                    "-B",
                    "-m",
                    "http.server",
                    "8999",
                    "--bind",
                    "127.0.0.1",
                    "--directory",
                    "/tmp",
                )
                await run(
                    "docker",
                    "exec",
                    api,
                    "python",
                    "-I",
                    "-c",
                    """
import time, urllib.request
for attempt in range(100):
    try:
        urllib.request.urlopen('http://127.0.0.1:8999/jwks.json', timeout=1).close()
        break
    except OSError:
        time.sleep(0.05)
else:
    raise AssertionError('test identity server did not start')
""",
                )

            try:
                await run(*command)
                api, broker = await container("api"), await container("broker")
                self.assertTrue(api and broker)
                inspections = json.loads(await run("docker", "inspect", api, broker))
                for record in inspections:
                    config_state, host = record["Config"], record["HostConfig"]
                    self.assertEqual(config_state["User"], f"{os.getuid()}:{os.getgid()}")
                    self.assertTrue(host["ReadonlyRootfs"])
                    self.assertEqual(
                        host["Memory"], (1024 if record is inspections[0] else 512) * 1024**2
                    )
                    self.assertEqual(host["MemorySwap"], host["Memory"])
                    self.assertEqual(host["NanoCpus"], 1_000_000_000)
                    self.assertEqual(host["PidsLimit"], 128)
                    self.assertEqual(host["CapDrop"], ["ALL"])
                    self.assertIn("no-new-privileges:true", host["SecurityOpt"])
                    self.assertEqual(host["LogConfig"]["Type"], "local")
                    self.assertEqual(
                        host["LogConfig"]["Config"], {"max-size": "10m", "max-file": "3"}
                    )
                    for flag in ("noexec", "nosuid", "nodev", "size=32m"):
                        self.assertIn(flag, host["Tmpfs"]["/tmp"])
                    self.assertIn({"Name": "core", "Soft": 0, "Hard": 0}, host["Ulimits"])
                self.assertEqual(inspections[1]["HostConfig"]["NetworkMode"], "none")
                self.assertEqual(inspections[1]["HostConfig"]["PortBindings"], {})
                self.assertEqual(
                    inspections[0]["HostConfig"]["PortBindings"]["8088/tcp"][0]["HostIp"],
                    "127.0.0.1",
                )
                self.assertEqual(
                    {m["Destination"] for m in inspections[0]["Mounts"]},
                    {"/data", "/etc/chipload/settings.json"},
                )
                self.assertEqual(
                    {m["Destination"] for m in inspections[1]["Mounts"]},
                    {"/data", "/var/run/docker.sock", "/etc/chipload/settings.json"},
                )
                await run(
                    "docker",
                    "exec",
                    api,
                    "python",
                    "-I",
                    "-c",
                    """
from pathlib import Path
import re
workers = []
for process in Path('/proc').iterdir():
    if not process.name.isdigit():
        continue
    try:
        argv = (process / 'cmdline').read_bytes().split(b'\\0')
        if len(argv) > 4 and argv[3] == b'-c' and argv[4].startswith(b'import resource,sys,os,select'):
            workers.append((process / 'limits').read_text())
    except FileNotFoundError:
        pass
assert len(workers) == 2, len(workers)
for limits in workers:
    assert re.search(r'Max address space\\s+268435456\\s+268435456', limits), limits
    assert re.search(r'Max open files\\s+64\\s+64', limits), limits
    assert re.search(r'Max core file size\\s+0\\s+0', limits), limits
""",
                )
                await run(
                    "docker",
                    "exec",
                    api,
                    "python",
                    "-I",
                    "-c",
                    """
import os, pathlib, shutil
assert os.geteuid() != 0
assert not pathlib.Path('/var/run/docker.sock').exists()
assert shutil.which('docker') is None
status = pathlib.Path('/proc/self/status').read_text()
assert 'CapEff:\t0000000000000000' in status
assert 'NoNewPrivs:\t1' in status
try:
    pathlib.Path('/opt/write-probe').write_text('forbidden')
except OSError:
    pass
else:
    raise AssertionError('image filesystem is writable')
""",
                )
                await run(
                    "docker",
                    "exec",
                    broker,
                    "python",
                    "-I",
                    "-c",
                    """
import socket
try:
    socket.create_connection(('1.1.1.1', 443), timeout=0.2)
except OSError:
    pass
else:
    raise AssertionError('broker has IP network access')
""",
                )
                await identity_server(api)
                async with httpx.AsyncClient(base_url=base, trust_env=False) as rest:
                    self.assertEqual(
                        (await rest.get("/v1/shops/thermal-example/source")).status_code, 401
                    )
                    self.assertEqual(
                        (
                            await rest.get(
                                "/v1/identity", headers={"Authorization": "Bearer invalid"}
                            )
                        ).status_code,
                        401,
                    )
                    async with httpx2.AsyncClient(
                        headers={"Authorization": "Bearer " + fixture.token()}, trust_env=False
                    ) as transport:
                        async with Client(
                            streamable_http_client(base + "/mcp", http_client=transport)
                        ) as agent:

                            async def call(tool, values):
                                result = await agent_call(agent, tool, values)
                                self.assertFalse(result.is_error, result)
                                return result.structured_content

                            async def build(revision, key, *, restart_broker=False):
                                job = await call(
                                    "build_request",
                                    {
                                        "shop_id": "thermal-example",
                                        "revision": revision,
                                        "request_key": key,
                                    },
                                )
                                if restart_broker:
                                    worker = "chipload-build-" + job["id"]
                                    for _ in range(100):
                                        ready = await asyncio.to_thread(
                                            subprocess.run,
                                            [
                                                "docker",
                                                "exec",
                                                worker,
                                                "python",
                                                "-c",
                                                "from pathlib import Path; assert Path('/tmp/runtime-test-ready').exists()",
                                            ],
                                            capture_output=True,
                                            timeout=5,
                                        )
                                        if ready.returncode == 0:
                                            break
                                        await asyncio.sleep(0.1)
                                    else:
                                        self.fail("worker did not reach the restart gate")
                                    # Deliver shutdown while the source worker is
                                    # blocked, then allow it to finish gracefully.
                                    await run("docker", "kill", "--signal=TERM", broker)
                                    await run(
                                        "docker",
                                        "exec",
                                        worker,
                                        "python",
                                        "-c",
                                        "from pathlib import Path; Path('/tmp/runtime-test-release').touch()",
                                    )
                                    await run("docker", "restart", "--time", "30", broker)
                                for _ in range(150):
                                    state = await call(
                                        "build_inspect",
                                        {"shop_id": "thermal-example", "build_id": job["id"]},
                                    )
                                    if state["state"] in {"failed", "succeeded"}:
                                        return state
                                    await asyncio.sleep(0.2)
                                self.fail("broker did not complete the bounded build")

                            valid = project().model_dump(mode="json")
                            # Synchronize shutdown with actual source execution;
                            # MCP program/container overhead must not race a sleep.
                            gate = """
from pathlib import Path
import time
Path('/tmp/runtime-test-ready').touch()
deadline = time.monotonic() + 20
while not Path('/tmp/runtime-test-release').exists():
    if time.monotonic() >= deadline:
        raise TimeoutError('restart test gate was not released')
    time.sleep(0.05)
"""
                            valid["files"][0]["content"] += gate
                            broken = {
                                **valid,
                                "files": [{"path": "thermal.py", "content": "def broken(:\n"}],
                            }
                            saved = await call(
                                "source_save", {"project": broken, "expected_head": None}
                            )
                            failed = await build(saved["revision"], "broken")
                            self.assertEqual(failed["state"], "failed")
                            self.assertEqual(
                                failed["report"]["diagnostics"][0]["path"], ["thermal.py", "1"]
                            )
                            repaired = await call(
                                "source_save",
                                {"project": valid, "expected_head": saved["revision"]},
                            )
                            first = await build(
                                repaired["revision"], "repaired", restart_broker=True
                            )
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
                            original = (await rest.get("/v1/shops/thermal-example/catalog")).json()
                            self.assertEqual(
                                original, await call("shop_catalog", {"shop_id": "thermal-example"})
                            )
                            denied = await rest.get(
                                "/v1/shops/other-shop/source",
                                headers={"Authorization": "Bearer " + fixture.token()},
                            )
                            self.assertEqual(denied.status_code, 403)

                            valid["files"][0]["content"] = (
                                valid["files"][0]["content"]
                                .removesuffix(gate)
                                .replace("Thermal Processing Example", "Updated in containers")
                            )
                            edited = await call(
                                "source_save",
                                {"project": valid, "expected_head": repaired["revision"]},
                            )
                            second = await build(edited["revision"], "updated")
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
                                (await rest.get("/v1/shops/thermal-example/catalog")).json()[
                                    "shop"
                                ]["name"],
                                "Updated in containers",
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

                    await run("docker", "restart", api, broker)
                    for _ in range(100):
                        try:
                            response = await rest.get("/v1/shops/thermal-example/catalog")
                            if response.status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        await asyncio.sleep(0.1)
                    else:
                        self.fail("runtime did not recover after restart")
                    self.assertEqual(response.json(), original)
                    await identity_server(api)
                    withdrawn = await rest.put(
                        "/v1/shops/thermal-example/publication",
                        headers={"Authorization": "Bearer " + fixture.token()},
                        json={
                            "shop_id": "thermal-example",
                            "action": "withdraw",
                            "expected_generation": 3,
                        },
                    )
                    self.assertEqual(withdrawn.status_code, 200, withdrawn.text)
                    self.assertEqual(
                        (await rest.get("/v1/shops/thermal-example/catalog")).status_code, 404
                    )
                for path in (directory / "data").glob("chipload.db*"):
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            finally:
                # Only this randomly named test project is stopped; its bind-mounted
                # temporary data is removed by TemporaryDirectory after shutdown.
                command[1] = "down"
                await run(*command)


if __name__ == "__main__":
    unittest.main()
