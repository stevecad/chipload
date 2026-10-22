"""Real managed workload issuance, interrupted rotation, revocation and cold recovery."""

import base64
import hashlib
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

workload = script.load("workload_live", "scripts/secrets-workload")
recovery = script.load("workload_recovery_live", "scripts/secrets-recovery")


@unittest.skipUnless(os.environ.get("CHIPLOAD_SECRETS_TESTS") == "1", "requires Docker and OpenBao")
class WorkloadLiveTests(unittest.TestCase):
    def test_provision_lost_responses_rotation_recovery_and_revocation(self):
        with tempfile.TemporaryDirectory(prefix="chipload-workload-live-") as temporary:
            root = Path(temporary)
            name = "chipload-workload-" + uuid.uuid4().hex[:10]
            network = name + "-net"
            gpg, setup = fixture_args(root, name=name, network=network)
            state = setup.state

            def run(*command):
                return subprocess.check_output(
                    command, stderr=subprocess.PIPE, text=True, timeout=180
                )

            def service(action, **kwargs):
                return script.operate(SimpleNamespace(state=state, action=action, **kwargs))

            def request(method, path, body=None, token=None, allowed=()):
                value, _, _, container = script.deployment(state)
                return script.request(
                    state, value, container, method, path, body, token, allowed_statuses=allowed
                )

            run("docker", "network", "create", "--internal", network)
            try:
                script.configure(setup)
                service("up")
                service("initialize")
                bootstrap = script.read_json(state / "bootstrap.json")

                def decrypt(packet):
                    return (
                        subprocess.check_output(
                            [*gpg, "--decrypt"],
                            input=base64.b64decode(packet),
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
                service("unseal", share_file=shares[0])
                service("unseal", share_file=shares[1])
                operator = root / "operator-token"
                root_token = decrypt(bootstrap["root_token"])
                archive.write(operator, root_token)
                credentials = root / "credentials"
                credentials.mkdir(mode=0o700)
                archive.write(
                    credentials / "bao-ca.pem", (state / "certificates/ca.pem").read_bytes()
                )
                config = {
                    "id": "evidence",
                    "receiver_id": "evidence-service",
                    "origin": "https://receiver:8443",
                    "contracts": [
                        {
                            "shop_id": "example",
                            "operation_id": "review",
                            "command": "review",
                            "effect": "read",
                        }
                    ],
                    "secrets": {
                        "origin": "https://bao:8200",
                        "ca_file": "bao-ca.pem",
                        "kv_mount": "chipload",
                        "secret_path": "shops/example/evidence",
                        "policy": "evidence-adapter",
                    },
                }
                adapter, receiver = root / "adapter.json", root / "receiver-secret"
                archive.write(adapter, json.dumps(config))
                bearer = secrets.token_urlsafe(32)
                archive.write(receiver, bearer)
                args = SimpleNamespace(
                    state=state,
                    operator_token=operator,
                    action="provision",
                    adapter=adapter,
                    credentials=credentials,
                    receiver_secret=receiver,
                    secret_id_ttl=3600,
                )
                call = workload.API.call

                def lost_issuance(api, method, path, *args, **kwargs):
                    result = call(api, method, path, *args, **kwargs)
                    if path.endswith("/custom-secret-id"):
                        raise OSError("lost committed SecretID response")
                    return result

                with patch.object(workload.API, "call", new=lost_issuance):
                    with self.assertRaisesRegex(OSError, "committed"):
                        workload.operate(args)
                pending = script.read_json(state / workload.PENDING)
                expected_secret = pending["next_secret_id"]
                key = root / "recovery-key"
                archive.generate_key(key)
                backup = SimpleNamespace(state=state, key=key, archive=root / "backup.gpg")
                with self.assertRaisesRegex(ValueError, "workload"):
                    recovery.backup(backup)
                args.action = "resume"
                result = workload.operate(args)
                self.assertEqual(result["generation"], 1)
                self.assertEqual(workload.read_secret(credentials / "secret-id"), expected_secret)
                role = "auth/approle/role/evidence-adapter"
                self.assertEqual(
                    len(request("LIST", role + "/secret-id", token=root_token)[1]["data"]["keys"]),
                    1,
                )
                args.action, args.workload = "verify", "evidence-adapter"
                self.assertTrue(workload.operate(args)["verified"])
                profile = script.read_json(state / "workloads/evidence-adapter.json")
                for value in (bearer, expected_secret, root_token):
                    self.assertNotIn(value, (state / "workloads/evidence-adapter.json").read_text())
                image = (ROOT / ".local/executor-image").read_text().strip()
                code = """
import hashlib
from pathlib import Path
from chipload.remote_adapter import HttpAdapterConfig, receiver_token
config = HttpAdapterConfig.model_validate_json(Path('/adapter.json').read_bytes())
print(hashlib.sha256(receiver_token(config.secrets, Path('/credentials')).encode()).hexdigest())
"""

                def real_client():
                    return run(
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        network,
                        "--user",
                        f"{os.getuid()}:{os.getgid()}",
                        "--read-only",
                        "--cap-drop",
                        "ALL",
                        "--security-opt",
                        "no-new-privileges",
                        "--memory",
                        "256m",
                        "--pids-limit",
                        "32",
                        "--mount",
                        f"type=bind,source={credentials},target=/credentials,readonly",
                        "--mount",
                        f"type=bind,source={adapter},target=/adapter.json,readonly",
                        "--entrypoint",
                        "python",
                        image,
                        "-c",
                        code,
                    ).strip()

                self.assertEqual(real_client(), hashlib.sha256(bearer.encode()).hexdigest())
                # Lose local completion after the new credential is already visible.
                install = workload.install_file

                def lost_install(path, value, previous=None):
                    install(path, value, previous)
                    if path.name == "secret-id":
                        raise OSError("lost installed credential completion")

                args.action = "rotate"
                with patch.object(workload, "install_file", side_effect=lost_install):
                    with self.assertRaises(OSError):
                        workload.operate(args)
                rotated = workload.read_secret(credentials / "secret-id")
                self.assertNotEqual(rotated, expected_secret)
                self.assertEqual(real_client(), hashlib.sha256(bearer.encode()).hexdigest())
                args.action = "resume"
                self.assertEqual(workload.operate(args)["generation"], 2)
                self.assertEqual(workload.read_secret(credentials / "secret-id"), rotated)
                self.assertEqual(
                    request(
                        "POST",
                        "auth/approle/login",
                        {"role_id": profile["role_id"], "secret_id": expected_secret},
                        allowed=(400, 403),
                    )[0],
                    400,
                )

                # A second rotation loses the retirement response after remote commit.
                def lost_retirement(api, method, path, *args, **kwargs):
                    result = call(api, method, path, *args, **kwargs)
                    if path.endswith("/secret-id/destroy"):
                        raise OSError("lost retirement completion")
                    return result

                args.action = "rotate"
                with patch.object(workload.API, "call", new=lost_retirement):
                    with self.assertRaises(OSError):
                        workload.operate(args)
                args.action = "resume"
                self.assertEqual(workload.operate(args)["generation"], 3)
                self.assertEqual(
                    len(request("LIST", role + "/secret-id", token=root_token)[1]["data"]["keys"]),
                    1,
                )
                # Refuse externally broadened policy before issuing any credential.
                acl = "sys/policies/acl/evidence-adapter"
                request("PUT", acl, {"policy": 'path "*" { capabilities = ["read"] }'}, root_token)
                args.action = "verify"
                with self.assertRaisesRegex(ValueError, "policy changed"):
                    workload.operate(args)
                request("PUT", acl, {"policy": workload.policy(profile)}, root_token)
                # Use a non-root administrative token, then retire bootstrap root.
                admin_policy = "\n".join(
                    [
                        'path "sys/policies/acl/evidence-adapter" { capabilities = ["read", "update", "create"] }',
                        'path "auth/approle/role/evidence-adapter" { capabilities = ["read", "delete"] }',
                        'path "auth/approle/role/evidence-adapter/*" { capabilities = ["read", "update", "create", "list"] }',
                        'path "sys/capabilities" { capabilities = ["update"] }',
                        'path "auth/token/revoke-self" { capabilities = ["update"] }',
                    ]
                )
                request(
                    "PUT",
                    "sys/policies/acl/workload-operator",
                    {"policy": admin_policy},
                    root_token,
                )
                admin = request(
                    "POST",
                    "auth/token/create-orphan",
                    {
                        "policies": ["workload-operator"],
                        "no_default_policy": True,
                        "ttl": "20m",
                        "renewable": False,
                    },
                    root_token,
                )[1]["auth"]["client_token"]
                operator.write_text(admin)
                request("POST", "auth/token/revoke-self", token=root_token)
                args.action = "rotate"
                self.assertEqual(workload.operate(args)["generation"], 4)
                recovery.backup(backup)
                receipt = (state / "workloads/evidence-adapter.json").read_bytes()
                shutil.rmtree(state)
                state = root / "restored"
                backup.state = args.state = state
                recovery.restore(backup)
                self.assertEqual((state / "workloads/evidence-adapter.json").read_bytes(), receipt)
                service("up")
                service("unseal", share_file=shares[0])
                service("unseal", share_file=shares[2])
                # An unsealed restored node may not be active yet. Wait for
                # readiness before exercising the restored AppRole credential.
                deadline = time.monotonic() + 30
                while True:
                    status, health = request("GET", "sys/health", allowed=(429, 500))
                    if status == 200:
                        self.assertTrue(health["initialized"])
                        self.assertFalse(health["sealed"])
                        self.assertEqual(
                            health["cluster_id"],
                            script.read_json(state / "initialized.json")["cluster_id"],
                        )
                        break
                    self.assertLess(time.monotonic(), deadline, f"OpenBao readiness: HTTP {status}")
                    time.sleep(.2)
                args.action = "verify"
                self.assertTrue(workload.operate(args)["verified"])
                self.assertEqual(real_client(), hashlib.sha256(bearer.encode()).hexdigest())
                current = workload.read_secret(credentials / "secret-id")
                login_started = time.monotonic()
                login = request(
                    "POST",
                    "auth/approle/login",
                    {"role_id": profile["role_id"], "secret_id": current},
                )[1]["auth"]
                self.assertGreater(login["lease_duration"], 0)
                args.action = "revoke"
                self.assertEqual(workload.operate(args)["status"], "revoked")
                # Both an already-issued token and the credential are denied.
                self.assertLess(time.monotonic() - login_started, login["lease_duration"] - 1)
                self.assertEqual(
                    request(
                        "GET",
                        "chipload/data/shops/example/evidence",
                        token=login["client_token"],
                        allowed=(403,),
                    )[0],
                    403,
                )
                self.assertEqual(
                    request(
                        "POST",
                        "auth/approle/login",
                        {"role_id": profile["role_id"], "secret_id": current},
                        allowed=(400, 403),
                    )[0],
                    400,
                )
                command = [
                    sys.executable,
                    str(ROOT / "scripts/secrets-workload"),
                    "--state",
                    str(state),
                    "--operator-token",
                    str(operator),
                    "verify",
                    "--workload",
                    "evidence-adapter",
                ]
                rejected = subprocess.run(command, capture_output=True, text=True, timeout=30)
                self.assertEqual(rejected.returncode, 1)
                for value in (bearer, admin, root_token, current):
                    self.assertNotIn(value, rejected.stdout + rejected.stderr)
                    self.assertNotIn(value.encode(), (state / "audit/audit.jsonl").read_bytes())
            finally:
                if state.exists():
                    service("down")
                run("docker", "network", "rm", network)
                subprocess.run(
                    ["gpgconf", "--homedir", str(root / "gnupg"), "--kill", "gpg-agent"],
                    capture_output=True,
                )
