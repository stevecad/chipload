"""Rotate actual Keycloak/PostgreSQL credentials, interrupt, resume, and restore."""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from credentials_support import ROOT, archive
from credentials_support import credentials as script

from examples.export_project import project


@unittest.skipUnless(
    os.environ.get("CHIPLOAD_CREDENTIAL_TESTS") == "1" and shutil.which("gpg"),
    "requires TLS images, Docker, and GnuPG",
)
class CredentialLiveTests(unittest.TestCase):
    def test_real_cutovers_interrupted_commits_and_recovery_preserve_identity_and_shop(self):
        with tempfile.TemporaryDirectory(prefix="chipload-credentials-test-") as temporary:
            root = Path(temporary)
            state, restored = root / "state", root / "restored"
            names = ["chipload-credentials-test-" + uuid.uuid4().hex[:12] for _ in range(2)]
            sockets = [socket.socket(), socket.socket()]
            for listener in sockets:
                listener.bind(("127.0.0.1", 0))
            port, admin_port = (listener.getsockname()[1] for listener in sockets)
            for listener in sockets:
                listener.close()

            def cli(tool, target=state):
                return [sys.executable, str(ROOT / "scripts" / tool), "--state", str(target)]

            def run(*args, expected=0):
                result = subprocess.run(args, capture_output=True, text=True, timeout=360)
                self.assertEqual(result.returncode, expected, result.stderr)
                return result.stdout

            run(
                *cli("tls-stack"),
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
                run(*cli("tls-stack"), "up")
                values = json.loads((state / "deployment.json").read_bytes())
                connection = json.loads((state / "client.json").read_bytes())
                old_agent = (state / "secrets/agent-secret").read_text()
                old_database = (state / "secrets/db-password").read_text()
                token = script.expect(*script.agent_token(state, values, old_agent), 200)[
                    "access_token"
                ]
                identity = script.agent_identity(state, values, old_agent)
                self.assertEqual(identity["subject"], connection["subject"])
                runtime = (state / "runtime.json").read_bytes()

                def api(path, *, expected=200, **kwargs):
                    return script.expect(
                        *script.request(state, values, path, api=True, **kwargs), expected
                    )

                shop = "/v1/shops/thermal-example"
                saved = api(
                    shop + "/source",
                    method="PUT",
                    token=token,
                    data={"project": project().model_dump(mode="json"), "expected_head": None},
                )
                requested = api(
                    shop + "/builds",
                    expected=202,
                    method="POST",
                    token=token,
                    data={
                        "shop_id": "thermal-example",
                        "revision": saved["revision"],
                        "request_key": "credential-fixture",
                    },
                )
                for _ in range(120):
                    built = api(shop + "/builds/" + requested["id"], token=token)
                    if built["state"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.3)
                self.assertEqual(built["state"], "succeeded")
                published = api(
                    shop + "/publication",
                    method="PUT",
                    token=token,
                    data={
                        "shop_id": "thermal-example",
                        "action": "publish",
                        "build_id": built["id"],
                        "expected_generation": 0,
                    },
                )
                catalog = api(shop + "/catalog")
                keys = script.expect(
                    *script.request(
                        state, values, "/realms/chipload/protocol/openid-connect/certs"
                    ),
                    200,
                )

                wrong = root / "wrong-password"
                archive.write(wrong, "incorrect-password")
                run(
                    *cli("tls-credentials"),
                    "--operator-password-file",
                    str(wrong),
                    "rotate-agent",
                    expected=1,
                )
                self.assertFalse((state / script.MARKER).exists())

                # Lose the response after Keycloak commits, before either local secret changes.
                original_request = script.request

                def interrupted_request(*args, **kwargs):
                    result = original_request(*args, **kwargs)
                    if kwargs.get("method") == "PUT" and "secret" in (kwargs.get("data") or {}):
                        self.assertEqual(result[0], 204)
                        raise OSError("test interruption after committed client update")
                    return result

                with patch.object(script, "request", side_effect=interrupted_request):
                    with self.assertRaisesRegex(OSError, "test interruption"):
                        script.rotate(state, "agent")
                self.assertTrue((state / "secrets/agent-secret").read_text() == old_agent)
                pending = json.loads((state / script.MARKER).read_bytes())
                script.rejected(*script.agent_token(state, values, old_agent))
                self.assertEqual(script.agent_identity(state, values, pending["new"]), identity)
                run(*cli("tls-stack"), "up", expected=1)
                key, backup = root / "recovery-key", root / "backup.gpg"
                run(*cli("tls-recovery"), "keygen", "--key", str(key))
                run(
                    *cli("tls-recovery"),
                    "backup",
                    "--key",
                    str(key),
                    "--archive",
                    str(backup),
                    expected=1,
                )
                self.assertFalse(backup.exists())
                run(*cli("tls-credentials"), "resume")
                self.assertFalse((state / script.MARKER).exists())
                self.assertTrue((state / "secrets/agent-secret").read_text() == pending["new"])
                # Offline JWT verification deliberately accepts an already-issued token until expiry.
                self.assertEqual(api("/v1/identity", token=token), identity)

                # Exercise a fresh complete CLI cutover as well as a resumed one.
                operator_file = root / "operator-token"
                archive.write(
                    operator_file,
                    script.operator_token(
                        state,
                        values,
                        "admin",
                        state / "secrets/admin-password",
                    ),
                )
                output = run(
                    *cli("tls-credentials"),
                    "--operator-token-file",
                    str(operator_file),
                    "rotate-agent",
                )
                new_agent = (state / "secrets/agent-secret").read_text()
                self.assertNotIn(new_agent, output)
                self.assertTrue(new_agent != pending["new"])
                script.rejected(*script.agent_token(state, values, pending["new"]))
                script.rejected(*script.agent_token(state, values, old_agent))
                self.assertEqual(script.agent_identity(state, values, new_agent), identity)

                original_change = script.set_database_password

                def interrupted_database(*args):
                    original_change(*args)
                    raise OSError("test interruption after committed database update")

                with patch.object(
                    script, "set_database_password", side_effect=interrupted_database
                ):
                    with self.assertRaisesRegex(OSError, "test interruption"):
                        script.rotate(state, "database")
                self.assertTrue((state / "secrets/db-password").read_text() == old_database)
                pending = json.loads((state / script.MARKER).read_bytes())
                command, environment, _ = script.stack.deployment(state)
                self.assertTrue(script.database_accepts(command, environment, pending["new"]))
                self.assertFalse(script.database_accepts(command, environment, old_database))
                run(*cli("tls-stack"), "reload", expected=1)
                run(*cli("tls-credentials"), "resume")
                new_database = (state / "secrets/db-password").read_text()
                self.assertTrue(new_database == pending["new"])
                self.assertEqual((state / "secrets/db-password").stat().st_mode & 0o777, 0o444)
                self.assertFalse(script.database_accepts(command, environment, old_database))
                self.assertEqual(script.agent_identity(state, values, new_agent), identity)
                self.assertEqual((state / "runtime.json").read_bytes(), runtime)
                token = script.expect(*script.agent_token(state, values, new_agent), 200)[
                    "access_token"
                ]
                self.assertEqual(api(shop + "/source", token=token), saved)
                self.assertEqual(api(shop + "/publication", token=token), published)
                self.assertEqual(api(shop + "/catalog"), catalog)
                api("/v1/shops/another-shop/source", token=token, expected=403)

                # Rotated credentials and their receipt must survive coordinated recovery.
                run(*cli("tls-recovery"), "backup", "--key", str(key), "--archive", str(backup))
                run(*cli("tls-stack"), "down")
                run(
                    *cli("tls-recovery", restored),
                    "restore",
                    "--name",
                    names[1],
                    "--key",
                    str(key),
                    "--archive",
                    str(backup),
                )
                run(*cli("tls-stack", restored), "up")
                values = json.loads((restored / "deployment.json").read_bytes())
                self.assertEqual(script.agent_identity(restored, values, new_agent), identity)
                script.rejected(*script.agent_token(restored, values, old_agent))
                command, environment, _ = script.stack.deployment(restored)
                self.assertTrue(script.database_accepts(command, environment, new_database))
                self.assertFalse(script.database_accepts(command, environment, old_database))
                self.assertEqual(
                    (restored / "credentials.json").read_bytes(),
                    (state / "credentials.json").read_bytes(),
                )
                self.assertEqual(
                    script.expect(
                        *script.request(restored, values, shop + "/catalog", api=True), 200
                    ),
                    catalog,
                )
                self.assertEqual(
                    script.expect(
                        *script.request(
                            restored, values, "/realms/chipload/protocol/openid-connect/certs"
                        ),
                        200,
                    ),
                    keys,
                )
            finally:
                for target, name in zip((state, restored), names):
                    if target.exists():
                        run(*cli("tls-stack", target), "down")
                    volumes = run(
                        "docker",
                        "volume",
                        "ls",
                        "-q",
                        "--filter",
                        f"label=com.docker.compose.project={name}",
                    ).split()
                    if volumes:
                        run("docker", "volume", "rm", *volumes)
