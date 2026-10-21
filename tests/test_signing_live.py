"""Real Keycloak rotation, retained tokens, interrupted commits and encrypted restore."""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import jwt
from credentials_support import ROOT
from credentials_support import credentials as script


@unittest.skipUnless(
    os.environ.get("CHIPLOAD_CREDENTIAL_TESTS") == "1" and shutil.which("gpg"),
    "requires TLS images, Docker, and GnuPG",
)
class SigningLiveTests(unittest.TestCase):
    def test_rotation_overlap_retirement_interrupted_responses_and_restore(self):
        self.exercise(emergency=False)

    def test_emergency_revocation_stops_cached_api_and_survives_restore(self):
        self.exercise(emergency=True)

    def exercise(self, *, emergency):
        with tempfile.TemporaryDirectory(prefix="chipload-signing-test-") as temporary:
            root = Path(temporary)
            state, restored = root / "state", root / "restored"
            names = ["chipload-signing-test-" + uuid.uuid4().hex[:12] for _ in range(2)]
            sockets = [socket.socket(), socket.socket()]
            for listener in sockets:
                listener.bind(("127.0.0.1", 0))
            port, admin_port = (s.getsockname()[1] for s in sockets)
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
                secret = (state / "secrets/agent-secret").read_text()
                old = script.expect(*script.agent_token(state, values, secret), 200)["access_token"]
                identity = script.expect(
                    *script.request(state, values, "/v1/identity", token=old, api=True), 200
                )
                before = (state / "runtime.json").read_bytes()
                original = script.request

                def lose_create(*args, **kwargs):
                    result = original(*args, **kwargs)
                    if kwargs.get("method") == "POST" and args[2] == script.signing.PROVIDERS:
                        self.assertEqual(result[0], 201)
                        raise OSError("lost signing create response")
                    return result

                with patch.object(script, "request", side_effect=lose_create):
                    with self.assertRaisesRegex(OSError, "lost signing"):
                        script.rotate(state, "signing", retain_days=1)
                pending = json.loads((state / script.MARKER).read_bytes())
                self.assertEqual(
                    pending["signing"]["old_kid"], jwt.get_unverified_header(old)["kid"]
                )
                self.assertNotIn("privateKey", json.dumps(pending))
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
                run(*cli("tls-credentials"), "resume")
                record = json.loads((state / "signing.json").read_bytes())
                self.assertEqual(record["new"]["id"], pending["component"]["id"])
                self.assertEqual(record["retire_after"] - record["activated_at"], 86400)
                new = script.expect(*script.agent_token(state, values, secret), 200)["access_token"]
                self.assertEqual(jwt.get_unverified_header(new)["kid"], record["new_kid"])
                for token in (old, new):
                    self.assertEqual(
                        script.expect(
                            *script.request(state, values, "/v1/identity", token=token, api=True),
                            200,
                        ),
                        identity,
                    )
                    self.assertEqual(
                        script.request(
                            state, values, "/v1/shops/another/source", token=token, api=True
                        )[0],
                        403,
                    )
                run(*cli("tls-credentials"), "retire-signing", expected=1)
                self.assertFalse((state / script.MARKER).exists())

                def lose_disable(*args, **kwargs):
                    result = original(*args, **kwargs)
                    if kwargs.get("method") == "PUT" and kwargs.get("data", {}).get(
                        "config", {}
                    ).get("enabled") == ["false"]:
                        self.assertEqual(result[0], 204)
                        raise OSError("lost signing disable response")
                    return result

                if emergency:
                    # A replacement or unrelated key cannot be selected by mistake.
                    run(*cli("tls-credentials"), "--key-id", record["new_kid"], "revoke-signing", expected=1)
                    self.assertFalse((state / script.MARKER).exists())
                    self.assertLess(script.signing.now(), record["retire_after"])
                    with patch.object(script, "request", side_effect=lose_disable):
                        with self.assertRaisesRegex(OSError, "lost signing"):
                            script.rotate(state, "signing-revoke", key_id=record["old_kid"])
                    apis = [c for c in script.recovery.records("container", names[0])
                            if c["Config"]["Labels"].get("com.docker.compose.service") == "api"]
                    self.assertEqual(len(apis), 1)
                    self.assertFalse(apis[0]["State"]["Running"])
                    run(*cli("tls-stack"), "up", expected=1)
                    run(*cli("tls-recovery"), "backup", "--key", str(key), "--archive", str(backup), expected=1)
                else:
                    # Advance only the operator retention clock. Real tokens
                    # remain unexpired; rejection proves key removal, not expiry.
                    with patch.object(script.signing, "now", return_value=record["retire_after"]):
                        with patch.object(script, "request", side_effect=lose_disable):
                            with self.assertRaisesRegex(OSError, "lost signing"):
                                script.rotate(state, "signing-retire")
                run(*cli("tls-credentials"), "resume")
                self.assertGreater(
                    jwt.decode(old, options={"verify_signature": False})["exp"],
                    script.signing.now(),
                )
                self.assertEqual(
                    script.request(state, values, "/v1/identity", token=old, api=True)[0], 401
                )
                self.assertEqual(script.agent_identity(state, values, secret), identity)
                self.assertEqual((state / "runtime.json").read_bytes(), before)
                keys = script.expect(*script.request(state, values, script.signing.CERTS), 200)
                self.assertNotIn(record["old_kid"], {k["kid"] for k in keys["keys"]})
                receipt = (state / "signing.json").read_bytes()
                self.assertEqual(json.loads(receipt)["status"], "retired")
                if emergency:
                    revocation = json.loads(receipt)["revocation"]
                    self.assertEqual(revocation["key_id"], record["old_kid"])
                    self.assertLessEqual(revocation["requested_at"], revocation["completed_at"])
                    self.assertLess(revocation["completed_at"], record["retire_after"])
                    self.assertEqual(json.loads((state / "credentials.json").read_bytes())["kind"], "signing-revoke")
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
                restored_values = json.loads((restored / "deployment.json").read_bytes())
                self.assertEqual(script.agent_identity(restored, restored_values, secret), identity)
                self.assertEqual((restored / "signing.json").read_bytes(), receipt)
                self.assertEqual(
                    script.expect(
                        *script.request(restored, restored_values, script.signing.CERTS), 200
                    ),
                    keys,
                )
                self.assertEqual(
                    script.request(restored, restored_values, "/v1/identity", token=old, api=True)[
                        0
                    ],
                    401,
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
