"""Native OpenBao CLI recovery without Keycloak, before and after storage loss."""

import base64
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from secrets_support import ROOT, archive, fixture_args, script

recovery = script.load("emergency_recovery_live", "scripts/secrets-recovery")


@unittest.skipUnless(os.environ.get("CHIPLOAD_SECRETS_TESTS") == "1", "requires Docker and OpenBao")
class EmergencyLiveTests(unittest.TestCase):
    def test_independent_login_quorum_root_retirement_and_cold_restore(self):
        with tempfile.TemporaryDirectory(prefix="chipload-emergency-live-") as temporary:
            root = Path(temporary)
            name = "chipload-emergency-" + uuid.uuid4().hex[:10]
            network = name + "-net"
            gpg, setup = fixture_args(root, name=name, network=network)
            state = setup.state
            binary = root / "bao"
            home = root / "cli-home"
            home.mkdir(mode=0o700)
            sensitive = []

            def run(*command):
                return subprocess.check_output(command, stderr=subprocess.PIPE, timeout=180)

            def service(action, **kwargs):
                return script.operate(SimpleNamespace(state=state, action=action, **kwargs))

            def request(method, path, body=None, token=None, allowed=()):
                value, _, _, container = script.deployment(state)
                return script.request(
                    state, value, container, method, path, body, token, allowed_statuses=allowed
                )

            def ready():
                deadline = time.monotonic() + 30
                while True:
                    status, health = request("GET", "sys/health", allowed=(429, 500))
                    if status == 200:
                        self.assertTrue(health["initialized"] and not health["sealed"])
                        return
                    self.assertLess(time.monotonic(), deadline, f"OpenBao readiness: HTTP {status}")
                    time.sleep(.2)

            def cli(*command, token=None, stdin=None, succeeds=True):
                connection = service("status")
                env = {
                    "PATH": os.environ["PATH"], "HOME": str(home),
                    "BAO_ADDR": connection["operator_address"],
                    "BAO_TLS_SERVER_NAME": connection["tls_server_name"],
                    "BAO_CACERT": connection["ca_file"],
                    "BAO_MAX_RETRIES": "0", "BAO_CLIENT_TIMEOUT": "10s",
                }
                if token is not None:
                    env["BAO_TOKEN"] = token
                result = subprocess.run(
                    [str(binary), *command], env=env, input=stdin, capture_output=True, timeout=20
                )
                # Never put CLI output, tokens or share arguments into failure diagnostics.
                self.assertEqual(result.returncode == 0, succeeds,
                                 f"OpenBao {command[0]} exit {result.returncode}")
                return result.stdout

            def decrypt(packet):
                value = subprocess.check_output(
                    [*gpg, "--decrypt"], input=base64.b64decode(packet),
                    stderr=subprocess.DEVNULL, timeout=30,
                ).decode().strip()
                sensitive.append(value)
                return value

            def login(password_file, *, succeeds=True):
                output = cli(
                    "login", "-token-only", "-method=userpass", "-path=chipload-emergency",
                    "username=custodian", f"password=@{password_file}", succeeds=succeeds,
                )
                if succeeds:
                    token = output.decode().strip()
                    sensitive.append(token)
                    data = request("GET", "auth/token/lookup-self", token=token)[1]["data"]
                    self.assertEqual(data["policies"], ["chipload-emergency-root"])
                    self.assertTrue(0 < data["ttl"] <= 300)
                    self.assertEqual(data["explicit_max_ttl"], 300)
                    return token

            def deny(token):
                for method, path, body in (
                    ("GET", "chipload/data/emergency-test", None),
                    ("PUT", "chipload/data/emergency-test", {"data": {"value": "denied"}}),
                    ("PUT", "sys/policies/acl/chipload-emergency-root", {"policy": ""}),
                    ("POST", "auth/token/create", {"policies": ["root"]}),
                    ("POST", "auth/chipload-emergency/users/custodian", {"password": "denied"}),
                    ("POST", "sys/seal", {}),
                    ("DELETE", "sys/audit/chipload", None),
                ):
                    self.assertEqual(request(method, path, body, token, (403,))[0], 403)

            def ceremony(token, shares, *, exercise_errors=False):
                start = json.loads(cli(
                    "operator", "generate-root", "-init", "-format=json",
                    f"-pgp-key={setup.root_pgp_key}", token=token,
                ))
                nonce = start["nonce"]
                self.assertTrue(start["started"])
                self.assertEqual(start["progress"], 0)
                self.assertEqual(start["required"], 2)
                self.assertEqual(start["pgp_fingerprint"], bootstrap["root_fingerprint"].lower())
                # A lost init reply can be reconciled by reading the exact nonce and recipient.
                current = json.loads(cli("operator", "generate-root", "-status", "-format=json",
                                         token=token))
                self.assertEqual(current["nonce"], nonce)
                self.assertEqual(current["pgp_fingerprint"], start["pgp_fingerprint"])
                if exercise_errors:
                    cli("operator", "generate-root", "-nonce=" + str(uuid.uuid4()), "-",
                        token=token, stdin=shares[0].read_bytes(), succeeds=False)
                first = json.loads(cli(
                    "operator", "generate-root", "-format=json", "-nonce=" + nonce, "-",
                    token=token, stdin=shares[0].read_bytes(),
                ))
                self.assertEqual(first["progress"], 1)
                self.assertFalse(first["complete"])
                self.assertFalse(first["encoded_token"])
                if exercise_errors:
                    cli("operator", "generate-root", "-nonce=" + nonce, "-",
                        token=token, stdin=shares[0].read_bytes(), succeeds=False)
                complete = json.loads(cli(
                    "operator", "generate-root", "-format=json", "-nonce=" + nonce, "-",
                    token=token, stdin=shares[1].read_bytes(),
                ))
                self.assertTrue(complete["complete"])
                self.assertEqual(complete["nonce"], nonce)
                self.assertEqual(complete["pgp_fingerprint"], start["pgp_fingerprint"])
                issued = decrypt(complete["encoded_token"])
                lookup = request("GET", "auth/token/lookup-self", token=issued)[1]["data"]
                self.assertEqual(lookup["policies"], ["root"])
                self.assertEqual(lookup["ttl"], 0)
                # Status cannot recover a completed response; cancel does not revoke its token.
                current = json.loads(cli("operator", "generate-root", "-status", "-format=json",
                                         token=token))
                self.assertFalse(current["started"])
                self.assertFalse(current["encoded_token"])
                cli("operator", "generate-root", "-cancel", token=token)
                self.assertEqual(request("GET", "auth/token/lookup-self", token=issued)[0], 200)
                return issued

            run("docker", "network", "create", "--internal", network)
            try:
                script.configure(setup)
                service("up")
                service("initialize")
                bootstrap = script.read_json(state / "bootstrap.json")
                original_root = decrypt(bootstrap["root_token"])
                shares = []
                for index, packet in enumerate(bootstrap["keys_base64"]):
                    path = root / f"share-{index}"
                    archive.write(path, decrypt(packet))
                    shares.append(path)
                service("unseal", share_file=shares[0])
                service("unseal", share_file=shares[1])
                ready()
                value, _, _, container = script.deployment(state)
                run("docker", "cp", container["Id"] + ":/usr/bin/bao", str(binary))
                binary.chmod(0o700)
                self.assertIn(b"2.7.1+chipload.1", cli("version"))
                password = root / "emergency-password"
                archive.write(password, secrets.token_urlsafe(48))
                sensitive.append(password.read_text())
                cli("policy", "write", "chipload-emergency-root",
                    str(ROOT / "deploy/secrets/emergency-policy.hcl"), token=original_root)
                cli("auth", "enable", "-path=chipload-emergency", "-default-lease-ttl=300s",
                    "-max-lease-ttl=300s", "userpass", token=original_root)
                cli("write", "auth/chipload-emergency/users/custodian", f"password=@{password}",
                    "token_policies=chipload-emergency-root", "token_no_default_policy=true",
                    "token_ttl=300s", "token_max_ttl=300s", "token_explicit_max_ttl=300s",
                    "token_type=service", "token_period=0", "token_num_uses=0", token=original_root)
                request("POST", "sys/mounts/chipload", {"type": "kv", "options": {"version": "2"}},
                        original_root)
                secret = secrets.token_urlsafe(32)
                sensitive.append(secret)
                request("POST", "chipload/data/emergency-test", {"data": {"value": secret}},
                        original_root)
                emergency = login(password)
                deny(emergency)
                self.assertEqual(request("GET", "sys/generate-root-token/attempt", allowed=(403,))[0], 403)
                # The legacy unauthenticated ceremony stays disabled, even with root authentication.
                self.assertEqual(request("POST", "sys/generate-root/attempt", {}, original_root,
                                         (405,))[0], 405)
                cli("token", "revoke", "-self", token=original_root)
                self.assertEqual(request("GET", "auth/token/lookup-self", token=original_root,
                                         allowed=(403,))[0], 403)
                issued_root = ceremony(emergency, shares, exercise_errors=True)
                self.assertTrue(issued_root != original_root)
                recovered = request("GET", "chipload/data/emergency-test", token=issued_root)[1]
                self.assertTrue(recovered["data"]["data"]["value"] == secret)
                replacement = root / "replacement-password"
                archive.write(replacement, secrets.token_urlsafe(48))
                sensitive.append(replacement.read_text())
                cli("write", "auth/chipload-emergency/users/custodian/password",
                    f"password=@{replacement}", token=issued_root)
                login(password, succeeds=False)
                # Password rotation alone leaves already-issued sessions valid until expiry/revocation.
                self.assertEqual(request("GET", "auth/token/lookup-self", token=emergency)[0], 200)
                session = root / "emergency-token"
                archive.write(session, emergency)
                cli("write", "auth/token/revoke", f"token=@{session}", token=issued_root)
                self.assertEqual(request("GET", "auth/token/lookup-self", token=emergency,
                                         allowed=(403,))[0], 403)
                emergency = login(replacement)
                cli("token", "revoke", "-self", token=emergency)
                cli("token", "revoke", "-self", token=issued_root)
                self.assertEqual(request("GET", "auth/token/lookup-self", token=issued_root,
                                         allowed=(403,))[0], 403)
                cluster = script.read_json(state / "initialized.json")["cluster_id"]
                backup = SimpleNamespace(state=state, key=root / "backup-key", archive=root / "backup.gpg")
                archive.generate_key(backup.key)
                recovery.backup(backup)
                shutil.rmtree(state)
                state = root / "replacement"
                backup.state = state
                recovery.restore(backup)
                self.assertEqual(service("up"), {"initialized": True, "sealed": True})
                service("unseal", share_file=shares[1])
                service("unseal", share_file=shares[2])
                ready()
                self.assertEqual(script.read_json(state / "initialized.json")["cluster_id"], cluster)
                for revoked in (original_root, issued_root, emergency):
                    self.assertEqual(request("GET", "auth/token/lookup-self", token=revoked,
                                             allowed=(403,))[0], 403)
                login(password, succeeds=False)
                emergency = login(replacement)
                deny(emergency)
                restored_root = ceremony(emergency, shares[1:])
                self.assertTrue(request("GET", "chipload/data/emergency-test", token=restored_root)[1]
                                ["data"]["data"]["value"] == secret)
                cli("token", "revoke", "-self", token=restored_root)
                cli("token", "revoke", "-self", token=emergency)
                self.assertEqual(request("GET", "auth/token/lookup-self", token=restored_root,
                                         allowed=(403,))[0], 403)
                audit = (state / "audit/audit.jsonl").read_bytes()
                self.assertIn(b"sys/generate-root-token/update", audit)
                self.assertTrue(all(item.encode() not in audit for item in sensitive),
                                "audit contains plaintext recovery material")
                self.assertFalse((home / ".bao-token").exists())
            finally:
                if state.exists():
                    service("down")
                run("docker", "network", "rm", network)
                subprocess.run(["gpgconf", "--homedir", str(root / "gnupg"), "--kill", "gpg-agent"],
                               capture_output=True)
