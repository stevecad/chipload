"""Recover a real ACME account after storage loss and renew a running deployment."""

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

from acme_recovery_support import ROOT, acme, archive
from acme_support import authority, command, initialize
from credentials_support import credentials


@unittest.skipUnless(
    os.environ.get("CHIPLOAD_ACME_TESTS") == "1",
    "requires pinned ACME/TLS images, Docker and GnuPG",
)
class ACMERecoveryLiveTests(unittest.TestCase):
    def test_storage_loss_preserves_account_and_resumes_bound_delivery(self):
        with tempfile.TemporaryDirectory(prefix="chipload-acme-recovery-") as temporary:
            root = Path(temporary)
            with authority(root) as ca:
                issuer = root / "issuer"
                initialize(issuer, ca, renew_days=7)
                current, original_report = acme.issue(issuer)
                keys = acme.account_keys(issuer)
                registration_path = next((issuer / "client/accounts").rglob("account.json"))
                registration = registration_path.read_bytes()
                registration_relative = registration_path.relative_to(issuer)
                state = root / "deployment"
                name = "chipload-acme-recovery-" + uuid.uuid4().hex[:12]
                sockets = [socket.socket(), socket.socket()]
                for listener in sockets:
                    listener.bind(("127.0.0.1", 0))
                port, admin_port = (listener.getsockname()[1] for listener in sockets)
                for listener in sockets:
                    listener.close()
                cli = [sys.executable, str(ROOT / "scripts/tls-stack"), "--state", str(state)]
                command(
                    *cli,
                    "init",
                    "--name",
                    name,
                    "--api-host",
                    ca.hosts["api"],
                    "--identity-host",
                    ca.hosts["identity"],
                    "--port",
                    str(port),
                    "--admin-port",
                    str(admin_port),
                    "--certificates",
                    str(current / "certificates"),
                    "--ca-bundle",
                    str(current / "identity-ca.pem"),
                )
                key, output = root / "key", root / "backup.gpg"
                archive.generate_key(key)
                recovery_cli = [sys.executable, str(ROOT / "scripts/tls-acme-recovery")]
                options = ["--key", str(key), "--archive", str(output)]
                try:
                    command(*cli, "up")
                    values = json.loads((state / "deployment.json").read_bytes())
                    password = (state / "secrets/agent-secret").read_text()
                    original_identity = credentials.agent_identity(state, values, password)
                    with acme.operation_lock(issuer):
                        acme.delivery.bind(issuer, state, acme.load(issuer), acme.certificates)
                    before = acme.certificates.hashes(state)
                    crash_code = """
import os, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(sys.argv[1]) / 'tests'))
from acme_recovery_support import recovery
publish = recovery.publish
def interrupted(*args):
    if sys.argv[5] == 'after':
        publish(*args)
    os._exit(91)
recovery.publish = interrupted
recovery.backup(SimpleNamespace(state=Path(sys.argv[2]), key=Path(sys.argv[3]), archive=Path(sys.argv[4])))
"""
                    for phase in ("before", "after"):
                        result = subprocess.run(
                            [
                                sys.executable,
                                "-c",
                                crash_code,
                                str(ROOT),
                                str(issuer),
                                str(key),
                                str(output),
                                phase,
                            ],
                            capture_output=True,
                            timeout=180,
                        )
                        self.assertEqual(result.returncode, 91, result.stderr.decode())
                        self.assertEqual(output.exists(), phase == "after")
                        with acme.operation_lock(issuer):
                            acme.check_account(issuer)
                        self.assertEqual(acme.certificates.hashes(state), before)
                    checked = json.loads(
                        command(*recovery_cli, "--state", str(issuer), "inspect", *options)
                    )
                    self.assertEqual(
                        checked["certificate_sha256"], original_report["leaves"]["api"]["sha256"]
                    )
                    replacement = root / "replacement"
                    restore_cli = [*recovery_cli, "--state", str(replacement), "restore", *options]
                    existing = subprocess.run(restore_cli, capture_output=True, timeout=180)
                    self.assertEqual(existing.returncode, 1)
                    self.assertFalse(replacement.exists())
                    issuer_name = "chipload-acme-" + acme.identity(issuer)[:24]
                    issuer_container = json.loads(command("docker", "inspect", issuer_name))[0]
                    self.assertFalse(issuer_container["State"]["Running"])
                    shutil.rmtree(issuer)
                    retained = subprocess.run(restore_cli, capture_output=True, timeout=180)
                    self.assertEqual(retained.returncode, 1)
                    self.assertFalse(replacement.exists())
                    command("docker", "rm", issuer_container["Id"])
                    restored = json.loads(command(*restore_cli))
                    self.assertEqual(restored["backup_id"], checked["backup_id"])
                    self.assertFalse(restored["delivery_enabled"])
                    self.assertEqual(acme.account_keys(replacement), keys)
                    self.assertEqual(
                        (replacement / registration_relative).read_bytes(), registration
                    )
                    self.assertEqual(acme.certificates.hashes(state), before)
                    self.assertIsNone(
                        acme.delivery.preflight(
                            replacement, acme.load(replacement), acme.certificates
                        )
                    )
                    for path in replacement.rglob("*"):
                        if not path.is_symlink():
                            self.assertEqual(
                                path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600
                            )
                    issuer_cli = [
                        sys.executable,
                        str(ROOT / "scripts/tls-acme"),
                        "--state",
                        str(replacement),
                    ]
                    command(*issuer_cli, "bind", "--deployment", str(state))
                    renewed = json.loads(command(*issuer_cli, "run"))
                    self.assertNotEqual(
                        renewed["leaves"]["api"]["sha256"],
                        original_report["leaves"]["api"]["sha256"],
                    )
                    acme.certificates.verify_live(state, values, renewed)
                    self.assertEqual(acme.account_keys(replacement), keys)
                    self.assertEqual(
                        (replacement / registration_relative).read_bytes(), registration
                    )
                    self.assertEqual(
                        credentials.agent_identity(state, values, password), original_identity
                    )
                finally:
                    command(*cli, "down")
                    volumes = command(
                        "docker",
                        "volume",
                        "ls",
                        "-q",
                        "--filter",
                        f"label=com.docker.compose.project={name}",
                    ).split()
                    if volumes:
                        command("docker", "volume", "rm", *volumes)


if __name__ == "__main__":
    unittest.main()
