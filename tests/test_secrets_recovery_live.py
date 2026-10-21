"""Recover a real OpenBao store after original storage loss, without a root token."""

import base64
import contextlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from secrets_support import ROOT, archive, fixture_args, script
from secrets_upgrade_support import legacy_deployment

recovery = script.load("secrets_recovery_live", "scripts/secrets-recovery")


@unittest.skipUnless(os.environ.get("CHIPLOAD_SECRETS_TESTS") == "1", "requires Docker and OpenBao")
class SecretsRecoveryLiveTests(unittest.TestCase):
    def test_storage_loss_restore_original_quorum_policies_and_revoked_root(self):
        self.recovery_scenario()

    def test_legacy_image_upgrade_preserves_quorum_permissions_and_revocations(self):
        self.recovery_scenario(upgrade_image=True)

    def recovery_scenario(self, *, upgrade_image=False):
        with tempfile.TemporaryDirectory(prefix="chipload-secrets-dr-") as temporary:
            root = Path(temporary)
            name = "chipload-secrets-dr-" + uuid.uuid4().hex[:10]
            network = name + "-net"
            gpg, setup = fixture_args(root, name=name, network=network)
            state = setup.state
            compatibility = contextlib.ExitStack()
            self.addCleanup(compatibility.close)
            if upgrade_image:
                compatibility.enter_context(legacy_deployment(recovery.secrets_upgrades, script, recovery.stack))
                compatibility.enter_context(patch.object(
                    recovery, "contract", return_value=recovery.secrets_upgrades.SOURCE,
                ))

            def invoke(action, **kwargs):
                return script.operate(SimpleNamespace(state=state, action=action, **kwargs))

            def run(*command, expected=0):
                result = subprocess.run(command, capture_output=True, text=True, timeout=180)
                self.assertEqual(result.returncode, expected, result.stderr)
                return result.stdout

            def bao(method, path, data=None, token=None, *, allowed_statuses=()):
                value, _, _, container = script.deployment(state)
                return script.request(
                    state, value, container, method, path, data, token,
                    allowed_statuses=allowed_statuses,
                )

            run("docker", "network", "create", "--internal", network)
            try:
                script.configure(setup)
                invoke("up")
                if upgrade_image:
                    legacy_container = script.deployment(state)[3]
                    self.assertEqual(legacy_container["Config"]["Image"], recovery.secrets_upgrades.SOURCE["image"])
                    self.assertNotIn("org.chipload.secrets-sha256", legacy_container["Config"]["Labels"])
                invoke("initialize")
                bootstrap = script.read_json(state / "bootstrap.json")

                def decrypt(value):
                    return (
                        subprocess.check_output(
                            [*gpg, "--decrypt"],
                            input=base64.b64decode(value),
                            stderr=subprocess.DEVNULL,
                        )
                        .decode()
                        .strip()
                    )

                shares = []
                for index, packet in enumerate(bootstrap["keys_base64"]):
                    path = root / f"share-{index}"
                    archive.write(path, decrypt(packet))
                    shares.append(path)
                root_token = decrypt(bootstrap["root_token"])
                invoke("unseal", share_file=shares[0])
                invoke("unseal", share_file=shares[1])
                cluster = script.read_json(state / "initialized.json")["cluster_id"]
                bao(
                    "POST",
                    "sys/mounts/chipload",
                    {"type": "kv", "options": {"version": "2"}},
                    root_token,
                )
                bao("POST", "sys/auth/approle", {"type": "approle"}, root_token)
                secret = secrets.token_urlsafe(32)
                bao(
                    "POST",
                    "chipload/data/shops/example/receiver",
                    {"data": {"token": secret}},
                    root_token,
                )
                bao(
                    "PUT",
                    "sys/policies/acl/receiver",
                    {
                        "policy": 'path "chipload/data/shops/example/receiver" { capabilities = ["read"] }'
                    },
                    root_token,
                )
                bao(
                    "POST",
                    "auth/approle/role/receiver",
                    {
                        "token_policies": ["receiver"],
                        "token_no_default_policy": True,
                        "token_ttl": "60s",
                        "token_max_ttl": "60s",
                        "secret_id_ttl": "1h",
                    },
                    root_token,
                )
                role = bao("GET", "auth/approle/role/receiver/role-id", token=root_token)[1][
                    "data"
                ]["role_id"]
                credential = bao("POST", "auth/approle/role/receiver/secret-id", {}, root_token)[1][
                    "data"
                ]["secret_id"]
                bao("POST", "auth/token/revoke-self", token=root_token)
                with self.assertRaisesRegex(ValueError, "HTTP 403"):
                    bao("GET", "sys/mounts", token=root_token)
                key, output = root / "key", root / "backup.gpg"
                archive.generate_key(key)
                args = SimpleNamespace(state=state, key=key, archive=output)
                # First fail after actual removal, then fail after archive publication.
                with patch.object(recovery, "pack", side_effect=OSError("injected disk failure")):
                    with self.assertRaises(OSError):
                        recovery.backup(args)
                self.assertIsNone(script.deployment(state)[3])
                with self.assertRaisesRegex(ValueError, "incomplete backup"):
                    invoke("up")
                with patch.object(
                    recovery, "finish_backup", side_effect=OSError("lost completion")
                ):
                    with self.assertRaises(OSError):
                        recovery.backup(args)
                self.assertTrue(output.exists())
                recovery.backup(args)
                original_bootstrap = (state / "bootstrap.json").read_bytes()
                original_audit = (state / "audit/audit.jsonl").read_bytes()
                compatibility.close()
                # Delete the complete original deployment, not just its container.
                shutil.rmtree(state)
                state = root / "replacement"
                args.state = state
                cli = [
                    sys.executable,
                    str(ROOT / "scripts/secrets-recovery"),
                    "--state",
                    str(state),
                ]
                upgrade_args = []
                if upgrade_image:
                    run(*cli, "restore", "--key", str(key), "--archive", str(output), expected=1)
                    self.assertFalse(state.exists())
                    upgrade_args = ["--upgrade-image", recovery.secrets_upgrades.NAME]
                result = json.loads(
                    run(*cli, "restore", "--key", str(key), "--archive", str(output), *upgrade_args)
                )
                if upgrade_image:
                    self.assertEqual(result["image_upgrade"], recovery.secrets_upgrades.NAME)
                    self.assertEqual(script.read_json(state / "recovery.json")["image_upgrade"],
                                     recovery.secrets_upgrades.transition(
                                         recovery.secrets_upgrades.SOURCE, recovery.contract(), recovery.secrets_upgrades.NAME,
                                     ))
                self.assertEqual(result["cluster_id"], cluster)
                self.assertEqual((state / "bootstrap.json").read_bytes(), original_bootstrap)
                self.assertEqual((state / "audit/audit.jsonl").read_bytes(), original_audit)
                self.assertEqual(invoke("up"), {"initialized": True, "sealed": True})
                if upgrade_image:
                    upgraded = script.deployment(state)[3]
                    self.assertNotEqual(upgraded["Image"], legacy_container["Image"])
                    self.assertEqual(upgraded["Config"]["Image"], upgraded["Image"])
                    self.assertEqual(upgraded["Config"]["Labels"]["org.chipload.secrets-sha256"], script.IMAGE_SHA256)
                self.assertEqual(
                    bao("GET", "chipload/data/shops/example/receiver", token=root_token)[0], 503
                )
                self.assertTrue(invoke("unseal", share_file=shares[0])["sealed"])
                self.assertTrue(invoke("unseal", share_file=shares[0])["sealed"])
                self.assertFalse(invoke("unseal", share_file=shares[2])["sealed"])
                self.assertEqual(
                    script.read_json(state / "initialized.json")["cluster_id"], cluster
                )
                # Unsealing completes before the restored node is necessarily
                # active. Establish readiness before testing authorization.
                deadline = time.monotonic() + 30
                while True:
                    status, health = bao("GET", "sys/health", allowed_statuses=(429, 500))
                    if status == 200:
                        self.assertTrue(health["initialized"])
                        self.assertFalse(health["sealed"])
                        self.assertEqual(health["cluster_id"], cluster)
                        break
                    self.assertLess(time.monotonic(), deadline, f"OpenBao readiness: HTTP {status}")
                    time.sleep(.2)
                with self.assertRaisesRegex(ValueError, "HTTP 403"):
                    bao("GET", "sys/mounts", token=root_token)
                token = bao(
                    "POST", "auth/approle/login", {"role_id": role, "secret_id": credential}
                )[1]["auth"]["client_token"]
                self.assertEqual(
                    bao("GET", "chipload/data/shops/example/receiver", token=token)[1]["data"][
                        "data"
                    ]["token"],
                    secret,
                )
                for method, path, body in (
                    ("GET", "chipload/data/shops/other/receiver", None),
                    ("GET", "sys/mounts", None),
                    ("LIST", "chipload/metadata/shops", None),
                    ("POST", "chipload/data/shops/example/receiver", {"data": {"token": "denied"}}),
                ):
                    with self.assertRaisesRegex(ValueError, "HTTP 403"):
                        bao(method, path, body, token)
                for sensitive in (secret, root_token, credential):
                    self.assertNotIn(sensitive.encode(), (state / "audit/audit.jsonl").read_bytes())
                # A second cold backup captures the installed receipt and remains restorable.
                args.archive = root / "second.gpg"
                recovery.backup(args)
                args.state = root / "second-replacement"
                recovery.restore(args)
                state = args.state
                invoke("up")
                invoke("unseal", share_file=shares[1])
                self.assertFalse(invoke("unseal", share_file=shares[2])["sealed"])
            finally:
                if state.exists():
                    invoke("down")
                run("docker", "network", "rm", network)
                subprocess.run(
                    ["gpgconf", "--homedir", str(root / "gnupg"), "--kill", "gpg-agent"],
                    capture_output=True,
                )
                compatibility.close()
