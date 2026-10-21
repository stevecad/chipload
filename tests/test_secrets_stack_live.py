"""Real OpenBao TLS, encrypted quorum bootstrap, sealed restart and scoped delivery."""

import base64
import hashlib
import json
import os
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from secrets_support import ROOT, archive, fixture_args, script


@unittest.skipUnless(
    os.environ.get("CHIPLOAD_SECRETS_TESTS") == "1" and shutil.which("gpg"),
    "requires Docker, OpenBao and GnuPG",
)
class SecretsLiveTests(unittest.TestCase):
    def test_encrypted_quorum_bootstrap_tls_reload_restart_and_workload_denials(self):
        with tempfile.TemporaryDirectory(prefix="chipload-secrets-test-") as temporary:
            root = Path(temporary)
            name = "chipload-secrets-test-" + uuid.uuid4().hex[:10]
            network = name + "-network"
            gpg, args = fixture_args(root, name=name, network=network)
            state = args.state
            cli = [sys.executable, str(ROOT / "scripts/secrets-stack"), "--state", str(state)]

            def run(*arguments, expected=0):
                result = subprocess.run(arguments, capture_output=True, text=True, timeout=150)
                self.assertEqual(result.returncode, expected, result.stderr)
                return result.stdout

            def invoke(action, **kwargs):
                return script.operate(type(args)(state=state, action=action, **kwargs))

            run("docker", "network", "create", "--internal", network)
            try:
                script.configure(args)
                self.assertEqual(
                    json.loads(run(*cli, "up")), {"initialized": False, "sealed": True}
                )
                value, _, _, container = script.deployment(state)
                config, host = container["Config"], container["HostConfig"]
                self.assertEqual(config["User"], f"{os.getuid()}:{os.getgid()}")
                self.assertTrue(host["ReadonlyRootfs"])
                self.assertEqual(host["CapDrop"], ["ALL"])
                self.assertIn("no-new-privileges:true", host["SecurityOpt"])
                self.assertFalse(host["PortBindings"])
                self.assertEqual(host["Memory"], 512 * 1024**2)
                self.assertEqual(host["MemorySwap"], host["Memory"])
                self.assertEqual(set(container["NetworkSettings"]["Networks"]), {network})
                self.assertEqual(
                    {m["Destination"] for m in container["Mounts"] if m["Type"] == "bind"},
                    {"/bao/data", "/bao/audit", "/tls", "/etc/openbao/server.hcl"},
                )
                # Persist the encrypted response, then lose the local completion.
                replace = archive.replace_json

                def interrupted(path, value):
                    replace(path, value)
                    if path.name == "bootstrap.json":
                        raise OSError("test loss after durable encrypted material")

                with patch.object(script.archive, "replace_json", side_effect=interrupted):
                    with self.assertRaisesRegex(OSError, "test loss"):
                        invoke("initialize")
                run(*cli, "up", expected=1)
                run(*cli, "down")
                # Finish the durable receipt without needing a running container.
                run(*cli, "initialize")
                run(*cli, "up")
                run(*cli, "initialize", expected=1)
                value, _, _, container = script.deployment(state)
                initialized = json.loads((state / "bootstrap.json").read_bytes())

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

                shares = [decrypt(item) for item in initialized["keys_base64"]]
                root_token = decrypt(initialized["root_token"])
                for secret in (*shares, root_token):
                    self.assertNotIn(secret, (state / "bootstrap.json").read_text())
                share_files = []
                for index, share in enumerate(shares):
                    path = root / f"share-{index}"
                    archive.write(path, share)
                    share_files.append(path)
                self.assertTrue(invoke("unseal", share_file=share_files[0])["sealed"])
                self.assertTrue(invoke("unseal", share_file=share_files[0])["sealed"])
                self.assertFalse(invoke("unseal", share_file=share_files[1])["sealed"])
                health_command = [
                    "docker",
                    "exec",
                    container["Id"],
                    *config["Healthcheck"]["Test"][1:],
                ]
                run(*health_command)

                def bao(method, path, data=None, token=root_token):
                    return script.request(state, value, container, method, path, data, token)

                self.assertEqual(bao("GET", "sys/audit")[1]["data"]["chipload/"]["type"], "file")
                bao("POST", "sys/mounts/chipload", {"type": "kv", "options": {"version": "2"}})
                bao("POST", "sys/auth/approle", {"type": "approle"})
                bearer = secrets.token_urlsafe(32)
                bao("POST", "chipload/data/shops/example/receiver", {"data": {"token": bearer}})
                bao(
                    "PUT",
                    "sys/policies/acl/receiver",
                    {
                        "policy": 'path "chipload/data/shops/example/receiver" { capabilities = ["read"] }\npath "auth/token/revoke-self" { capabilities = ["update"] }'
                    },
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
                )
                role_id = bao("GET", "auth/approle/role/receiver/role-id")[1]["data"]["role_id"]
                secret_id = bao("POST", "auth/approle/role/receiver/secret-id", {})[1]["data"][
                    "secret_id"
                ]
                token = bao(
                    "POST", "auth/approle/login", {"role_id": role_id, "secret_id": secret_id}, None
                )[1]["auth"]["client_token"]
                self.assertEqual(
                    bao("GET", "chipload/data/shops/example/receiver", token=token)[1]["data"][
                        "data"
                    ]["token"],
                    bearer,
                )
                for method, path, data in (
                    ("GET", "sys/mounts", None),
                    ("GET", "chipload/data/shops/other/receiver", None),
                    ("LIST", "chipload/metadata/shops", None),
                    (
                        "POST",
                        "chipload/data/shops/example/receiver",
                        {"data": {"token": "forbidden"}},
                    ),
                ):
                    with self.assertRaisesRegex(script.SecretsError, "HTTP 403"):
                        bao(method, path, data, token)
                bao("POST", "auth/token/revoke-self", token=token)
                credentials = root / "workload"
                credentials.mkdir(mode=0o700)
                for filename, data in (
                    ("role-id", role_id),
                    ("secret-id", secret_id),
                    ("bao-ca.pem", (state / "certificates/ca.pem").read_bytes()),
                ):
                    archive.write(credentials / filename, data)
                executor_image = (ROOT / ".local/executor-image").read_text().strip()
                code = """
import hashlib, socket
from pathlib import Path
from chipload.remote_adapter import OpenBao, receiver_token
try:
    connection = socket.create_connection(('1.1.1.1', 443), timeout=0.2)
except OSError:
    pass
else:
    connection.close()
    raise RuntimeError('unexpected external route')
config = OpenBao(origin='https://bao:8200', ca_file='bao-ca.pem', kv_mount='chipload',
                 secret_path='shops/example/receiver', policy='receiver')
print(hashlib.sha256(receiver_token(config, Path('/credentials')).encode()).hexdigest())
"""
                probe = [
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
                    "512m",
                    "--memory-swap",
                    "512m",
                    "--pids-limit",
                    "32",
                    "--mount",
                    f"type=bind,src={credentials},dst=/credentials,readonly",
                    "--entrypoint",
                    "python",
                    executor_image,
                    "-c",
                    code,
                ]
                self.assertEqual(run(*probe).strip(), hashlib.sha256(bearer.encode()).hexdigest())
                audit = (state / "audit/audit.jsonl").read_text()
                self.assertIn("auth/approle/login", audit)
                for private in (root_token, secret_id, token, bearer):
                    self.assertNotIn(private, audit)

                # A new leaf under the same CA must actually be loaded, not just trusted.
                crypto = script.tls
                previous = crypto.x509.load_pem_x509_certificate(
                    (state / "certificates/server.crt").read_bytes()
                )
                ca_key = crypto.serialization.load_pem_private_key(
                    (root / "tls/recovery/local-ca-key.pem").read_bytes(), password=None
                )
                builder = (
                    crypto.x509.CertificateBuilder()
                    .subject_name(previous.subject)
                    .issuer_name(previous.issuer)
                    .public_key(previous.public_key())
                    .serial_number(crypto.x509.random_serial_number())
                    .not_valid_before(previous.not_valid_before_utc)
                    .not_valid_after(previous.not_valid_after_utc)
                )
                for extension in previous.extensions:
                    builder = builder.add_extension(extension.value, extension.critical)
                leaf = builder.sign(ca_key, crypto.hashes.SHA256())
                (state / "certificates/server.crt").write_bytes(
                    leaf.public_bytes(crypto.serialization.Encoding.PEM)
                )
                with self.assertRaises(ssl.SSLError):
                    invoke("status")
                self.assertFalse(invoke("reload")["sealed"])
                self.assertEqual(run(*probe).strip(), hashlib.sha256(bearer.encode()).hexdigest())
                (state / "audit/audit.jsonl").rename(state / "audit/previous.jsonl")
                invoke("reload")
                bao("GET", "sys/audit")
                self.assertTrue((state / "audit/audit.jsonl").stat().st_size)
                self.assertTrue((state / "audit/previous.jsonl").stat().st_size)

                # Replace the CA and leaf, then prove SIGHUP installed the new leaf.
                renewed = root / "renewed"
                script.tls.local_certificates(renewed, {"server": "bao"})
                for source, target in (
                    (renewed / "certificates/server.crt", "server.crt"),
                    (renewed / "certificates/server.key", "server.key"),
                    (renewed / "identity-ca.pem", "ca.pem"),
                ):
                    (state / "certificates" / target).write_bytes(source.read_bytes())
                self.assertFalse(invoke("reload")["sealed"])
                run(*probe, expected=1)  # The previous CA no longer authenticates the server.
                (credentials / "bao-ca.pem").write_bytes(
                    (state / "certificates/ca.pem").read_bytes()
                )
                self.assertEqual(run(*probe).strip(), hashlib.sha256(bearer.encode()).hexdigest())
                self.assertEqual(
                    bao("GET", "chipload/data/shops/example/receiver")[1]["data"]["data"]["token"],
                    bearer,
                )
                run(*cli, "down")
                self.assertTrue(json.loads(run(*cli, "up"))["sealed"])
                value, _, _, container = script.deployment(state)
                run(
                    "docker",
                    "exec",
                    container["Id"],
                    *config["Healthcheck"]["Test"][1:],
                    expected=2,
                )
                self.assertEqual(bao("GET", "chipload/data/shops/example/receiver")[0], 503)
                run(*probe, expected=1)
                invoke("unseal", share_file=share_files[0])
                invoke("unseal", share_file=share_files[2])
                self.assertEqual(
                    bao("GET", "chipload/data/shops/example/receiver")[1]["data"]["data"]["token"],
                    bearer,
                )
                # Missing initialized storage cannot turn into a fresh empty server.
                run(*cli, "down")
                (state / "data").rename(state / "preserved-data")
                run(*cli, "up", expected=1)
                self.assertFalse((state / "data").exists())
                (state / "preserved-data").rename(state / "data")
                # Incorrect recorded identity stops the owned container on unseal.
                receipt = script.read_json(state / "initialized.json")
                self.assertTrue(receipt["cluster_id"])
                archive.replace_json(
                    state / "initialized.json", {**receipt, "cluster_id": "wrong-cluster"}
                )
                run(*cli, "up")
                invoke("unseal", share_file=share_files[0])
                with self.assertRaisesRegex(script.SecretsError, "cluster identity differs"):
                    invoke("unseal", share_file=share_files[1])
                self.assertFalse(script.deployment(state)[3]["State"]["Running"])
            finally:
                if state.exists():
                    run(*cli, "down")
                run("docker", "network", "rm", network)
                subprocess.run(
                    ["gpgconf", "--homedir", str(root / "gnupg"), "--kill", "gpg-agent"],
                    capture_output=True,
                )
