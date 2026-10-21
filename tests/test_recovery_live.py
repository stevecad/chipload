"""Restore real identity and published shop state after an interrupted recovery."""

import asyncio
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from recovery_support import ROOT, archive, script

from chipload.lifecycle import Repository
from chipload.quotas import QuotaPolicy
from chipload.request_limits import RequestLimiter
from examples.export_project import project
from examples.heat_treatment import observations


@unittest.skipUnless(
    os.environ.get("CHIPLOAD_RECOVERY_TESTS") == "1" and shutil.which("gpg"),
    "requires TLS images, Docker, and GnuPG",
)
class RecoveryLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_encrypted_snapshot_interrupted_restore_and_real_identity_lifecycle(self):
        with tempfile.TemporaryDirectory(prefix="chipload-recovery-test-") as temporary:
            root = Path(temporary)
            original, restored = root / "original", root / "restored"
            names = ["chipload-recovery-test-" + uuid.uuid4().hex[:12] for _ in range(2)]
            sockets = [socket.socket(), socket.socket()]
            for listener in sockets:
                listener.bind(("127.0.0.1", 0))
            port, admin_port = (listener.getsockname()[1] for listener in sockets)
            for listener in sockets:
                listener.close()

            def cli(state):
                return [sys.executable, str(ROOT / "scripts/tls-stack"), "--state", str(state)]

            def recovery(state):
                return [sys.executable, str(ROOT / "scripts/tls-recovery"), "--state", str(state)]

            async def run(*arguments, expected=0):
                result = await asyncio.to_thread(
                    subprocess.run, list(arguments), capture_output=True, text=True, timeout=300
                )
                self.assertEqual(result.returncode, expected, result.stderr)
                return result.stdout

            await run(
                *cli(original),
                "init",
                "--local",
                "--name",
                names[0],
                "--port",
                str(port),
                "--admin-port",
                str(admin_port),
                "--shop",
                "thermal-example",
            )
            try:
                await run(*cli(original), "up")
                connection = json.loads((original / "client.json").read_bytes())
                context = ssl.create_default_context(cafile=connection["ca_file"])
                base = connection["resource_url"].removesuffix("/mcp")
                shop = base + "/v1/shops/thermal-example"
                async with httpx.AsyncClient(verify=context, trust_env=False, timeout=20) as client:

                    async def token():
                        response = await client.post(
                            connection["issuer"] + "/protocol/openid-connect/token",
                            data={
                                "grant_type": "client_credentials",
                                "client_id": connection["client_id"],
                                "client_secret": Path(connection["client_secret_file"]).read_text(),
                            },
                        )
                        self.assertEqual(response.status_code, 200)
                        return {"Authorization": "Bearer " + response.json()["access_token"]}

                    headers = await token()
                    identity = (await client.get(base + "/v1/identity", headers=headers)).json()
                    keys = (
                        await client.get(connection["issuer"] + "/protocol/openid-connect/certs")
                    ).json()
                    saved = await client.put(
                        shop + "/source",
                        headers=headers,
                        json={
                            "project": project().model_dump(mode="json"),
                            "expected_head": None,
                        },
                    )
                    self.assertEqual(saved.status_code, 200)
                    revision = saved.json()["revision"]
                    requested = await client.post(
                        shop + "/builds",
                        headers=headers,
                        json={
                            "shop_id": "thermal-example",
                            "revision": revision,
                            "request_key": "backup-fixture",
                        },
                    )
                    self.assertEqual(requested.status_code, 202)
                    build_id = requested.json()["id"]
                    for _ in range(120):
                        build = (
                            await client.get(shop + "/builds/" + build_id, headers=headers)
                        ).json()
                        if build["state"] in {"failed", "succeeded"}:
                            break
                        await asyncio.sleep(0.3)
                    self.assertEqual(build["state"], "succeeded", build["report"])
                    published = await client.put(
                        shop + "/publication",
                        headers=headers,
                        json={
                            "shop_id": "thermal-example",
                            "action": "publish",
                            "build_id": build_id,
                            "expected_generation": 0,
                        },
                    )
                    self.assertEqual(published.status_code, 200)
                    seen = await client.put(
                        shop + "/observations",
                        headers=headers,
                        json={
                            "shop_id": "thermal-example",
                            "expected_revision": None,
                            "observations": [
                                item.model_dump(mode="json") for item in observations()
                            ],
                        },
                    )
                    self.assertEqual(seen.status_code, 200)
                    catalog = (await client.get(shop + "/catalog")).json()
                    audit = (await client.get(shop + "/audit", headers=headers)).json()
                    repository = Repository(original / "data/chipload.db")
                    policy = repository.read_quotas()["policy"]
                    policy["shop"]["pending_builds"] = 7
                    # Keep spent identity credit outstanding through maintenance;
                    # an old fixed date lets normal bucket pruning erase the canary.
                    policy["requests"]["identity"]["per_minute"] = 1
                    repository.set_quotas(QuotaPolicy.model_validate(policy), expected_generation=0)
                    fixed = time.time_ns() // 1_000_000
                    limiter = RequestLimiter(repository, clock=lambda: fixed)
                    for _ in range(policy["requests"]["identity"]["burst"]):
                        limiter.identity("recovery-canary")
                    key, backup = root / "key", root / "snapshot.gpg"
                    await run(*recovery(original), "keygen", "--key", str(key))
                    await run(
                        *recovery(original), "backup", "--key", str(key), "--archive", str(backup)
                    )
                    self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
                    self.assertFalse((original / script.BACKUP_MARKER).exists())
                    details = json.loads(
                        await run(
                            *recovery(original),
                            "inspect",
                            "--key",
                            str(key),
                            "--archive",
                            str(backup),
                        )
                    )
                    self.assertIn("data/chipload.db.admission", details["files"])
                    self.assertIn("commit", details["checkout"])
                    self.assertEqual((await client.get(shop + "/catalog")).json(), catalog)
                    # Abrupt death after snapshots must not publish a partial archive or allow startup.
                    crash_output = root / "interrupted.gpg"
                    crash = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from recovery_support import script, archive
snapshot = archive.snapshot_pair
def interrupted(*arguments, **options):
    snapshot(*arguments, **options)
    os._exit(99)
archive.snapshot_pair = interrupted
script.backup(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), archive.MAX_BYTES)
"""
                    await run(
                        sys.executable,
                        "-c",
                        crash,
                        str(ROOT / "tests"),
                        str(original),
                        str(crash_output),
                        str(key),
                        expected=99,
                    )
                    self.assertFalse(crash_output.exists())
                    self.assertTrue((original / script.BACKUP_MARKER).exists())
                    await run(*cli(original), "up", expected=1)
                    await run(*recovery(original), "resume-backup")
                    self.assertFalse((original / script.BACKUP_MARKER).exists())
                    self.assertEqual((await client.get(shop + "/catalog")).json(), catalog)
                    headers = await token()
                    withdrawn = await client.put(
                        shop + "/publication",
                        headers=headers,
                        json={
                            "shop_id": "thermal-example",
                            "action": "withdraw",
                            "expected_generation": 1,
                        },
                    )
                    self.assertEqual(withdrawn.status_code, 200)
                    self.assertEqual((await client.get(shop + "/catalog")).status_code, 404)
                    await run(*cli(original), "down")

                    real_compose = script.compose

                    def interrupt_after_commit(*arguments, **options):
                        result = real_compose(*arguments, **options)
                        if "pg_restore" in arguments:
                            raise RuntimeError("simulated interruption after PostgreSQL committed")
                        return result

                    with patch.object(script, "compose", side_effect=interrupt_after_commit):
                        with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                            await asyncio.to_thread(
                                script.restore,
                                restored,
                                backup,
                                key,
                                names[1],
                                False,
                                archive.MAX_BYTES,
                            )
                    self.assertTrue((restored / script.RESTORE_MARKER).exists())
                    await run(*cli(restored), "up", expected=1)
                    await run(
                        *recovery(restored),
                        "restore",
                        "--key",
                        str(key),
                        "--archive",
                        str(backup),
                        "--name",
                        names[1],
                        "--resume",
                    )
                    self.assertFalse((restored / script.RESTORE_MARKER).exists())
                    self.assertEqual(
                        json.loads((restored / "restore.json").read_bytes())["backup_id"],
                        details["backup_id"],
                    )
                    running = await run(
                        "docker",
                        "ps",
                        "-q",
                        "--filter",
                        f"label=com.docker.compose.project={names[1]}",
                    )
                    self.assertEqual(running.strip(), "")
                    paths = (restored / "data/chipload.db", restored / "data/chipload.db.admission")
                    for path in paths:
                        with closing(sqlite3.connect(path)) as database:
                            self.assertEqual(
                                database.execute("PRAGMA journal_mode").fetchone()[0], "delete"
                            )
                    with closing(archive.readonly(paths[1])) as database:
                        generation, saved_policy = database.execute(
                            "SELECT generation,policy FROM quota_policy WHERE id=1"
                        ).fetchone()
                        self.assertEqual(generation, 1)
                        self.assertEqual(json.loads(saved_policy), policy)
                        spent = database.execute(
                            "SELECT credit,updated_ms FROM request_buckets WHERE scope='identity' AND subject=?",
                            (hashlib.sha256(b"recovery-canary").digest(),),
                        ).fetchone()
                        self.assertEqual(spent, (0, fixed))
                    restored_client = json.loads((restored / "client.json").read_bytes())
                    self.assertEqual(
                        restored_client["client_secret_file"],
                        str(restored / "secrets/agent-secret"),
                    )
                    await run(*cli(restored), "up")
                    for path in paths:
                        with closing(sqlite3.connect(path)) as database:
                            self.assertEqual(
                                database.execute("PRAGMA journal_mode").fetchone()[0], "wal"
                            )
                    connection = restored_client
                    headers = await token()
                    self.assertEqual(
                        (await client.get(base + "/v1/identity", headers=headers)).json(), identity
                    )
                    new_keys = (
                        await client.get(connection["issuer"] + "/protocol/openid-connect/certs")
                    ).json()
                    self.assertEqual(
                        {k["kid"]: k for k in new_keys["keys"]}, {k["kid"]: k for k in keys["keys"]}
                    )
                    self.assertEqual((await client.get(shop + "/catalog")).json(), catalog)
                    self.assertEqual(
                        (await client.get(shop + "/source", headers=headers)).json()["revision"],
                        revision,
                    )
                    self.assertEqual(
                        (await client.get(shop + "/builds/" + build_id, headers=headers)).json(),
                        build,
                    )
                    self.assertEqual(
                        (await client.get(shop + "/observations", headers=headers)).json(),
                        seen.json(),
                    )
                    self.assertEqual(
                        (await client.get(shop + "/audit", headers=headers)).json(), audit
                    )
                    self.assertEqual(
                        (
                            await client.get(base + "/v1/shops/other/source", headers=headers)
                        ).status_code,
                        403,
                    )
                    async with httpx2.AsyncClient(verify=context, trust_env=False) as transport:
                        async with Client(
                            streamable_http_client(
                                connection["resource_url"], http_client=transport
                            )
                        ) as agent:
                            result = await agent.call_tool(
                                "discover_shop", {"request": {"shop_id": "thermal-example"}}
                            )
                            self.assertFalse(result.is_error)
                            self.assertEqual(result.structured_content, catalog)
                    # A new mutation uses the restored publication generation, not the later withdrawn one.
                    changed = await client.put(
                        shop + "/publication",
                        headers=headers,
                        json={
                            "shop_id": "thermal-example",
                            "action": "withdraw",
                            "expected_generation": 1,
                        },
                    )
                    self.assertEqual(changed.status_code, 200)
            finally:
                for state, name in zip((original, restored), names):
                    if state.exists():
                        await run(*cli(state), "down")
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
