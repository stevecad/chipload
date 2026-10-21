"""Real ACME issuance and renewal with HTTP validation through a published port."""

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

from acme_support import ROOT, acme, authority, command, initialize
from credentials_support import credentials


@unittest.skipUnless(
    os.environ.get("CHIPLOAD_ACME_TESTS") == "1", "requires pinned ACME images and Docker"
)
class ACMELiveTests(unittest.TestCase):
    def test_issue_and_renew_with_real_domain_validation(self):
        with tempfile.TemporaryDirectory(prefix="chipload-acme-live-") as temporary:
            root = Path(temporary)
            with authority(root) as ca:
                state = root / 'issuer, $USER %n "quoted"'
                initialize(state, ca, renew_days=7)
                first, report = acme.issue(state)
                self.assertEqual(report["leaves"]["api"]["hostname"], ca.hosts["api"])
                self.assertEqual(report["leaves"]["identity"]["hostname"], ca.hosts["identity"])
                second, renewed = acme.issue(state)
                self.assertNotEqual(first, second)
                self.assertNotEqual(
                    report["leaves"]["api"]["sha256"], renewed["leaves"]["api"]["sha256"]
                )
                self.assertTrue((state / "current").is_symlink())
                self.assertEqual((state / "current").resolve(), second)
                receipt = json.loads((state / "last-run.json").read_bytes())
                self.assertTrue(receipt["succeeded"])
                self.assertFalse((state / acme.MARKER).exists())
                for path in (state / "client").rglob("*"):
                    self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
                name = "chipload-acme-" + acme.identity(state)[:24]
                record = json.loads(command("docker", "inspect", name))[0]
                self.assertFalse(record["State"]["Running"])
                self.assertTrue(record["HostConfig"]["ReadonlyRootfs"])
                self.assertEqual(record["HostConfig"]["CapDrop"], ["ALL"])
                self.assertIn("no-new-privileges", record["HostConfig"]["SecurityOpt"])
                self.assertEqual(record["HostConfig"]["Memory"], 256 * 1024 * 1024)
                self.assertEqual(record["HostConfig"]["PidsLimit"], 64)
                self.assertEqual(record["Config"]["User"], f"{os.getuid()}:{os.getgid()}")
                self.assertEqual(
                    {mount["Destination"] for mount in record["Mounts"] if mount["Type"] == "bind"},
                    {"/data", "/config/client.json", "/config/directory-ca.pem"},
                )
                keys = list((state / "client/accounts").rglob("*.key"))
                self.assertEqual(len(keys), 1)
                keys[0].unlink()
                with self.assertRaisesRegex(ValueError, "key storage changed"):
                    acme.issue(state)
                current = json.loads(command("docker", "inspect", name))[0]
                self.assertEqual(record["State"]["StartedAt"], current["State"]["StartedAt"])

    def test_no_renewal_when_not_due_and_ca_failure_preserves_published_material(self):
        with tempfile.TemporaryDirectory(prefix="chipload-acme-failure-") as temporary:
            root = Path(temporary)
            with authority(root) as ca:
                state = root / "issuer"
                initialize(state, ca)
                first, report = acme.issue(state)
                second, no_change = acme.issue(state)
                self.assertEqual(first, second)
                self.assertEqual(report["leaves"], no_change["leaves"])
                before = (state / "issued.json").read_bytes()
                command("docker", "stop", ca.name)
                with self.assertRaisesRegex(ValueError, "ACME client failed"):
                    acme.issue(state)
                self.assertEqual(before, (state / "issued.json").read_bytes())
                self.assertEqual((state / "current").resolve(), first)
                self.assertFalse((state / acme.MARKER).exists())
                self.assertFalse(json.loads((state / "last-run.json").read_bytes())["succeeded"])
                name = "chipload-acme-" + acme.identity(state)[:24]
                record = json.loads(command("docker", "inspect", name))[0]
                command("docker", "update", "--memory", "512m", "--memory-swap", "512m", name)
                with self.assertRaisesRegex(ValueError, "restrictions changed"):
                    acme.issue(state)
                self.assertEqual(
                    record["State"]["StartedAt"],
                    json.loads(command("docker", "inspect", name))[0]["State"]["StartedAt"],
                )

    @unittest.skipUnless(shutil.which("systemctl"), "requires a running user systemd manager")
    def test_interrupted_delivery_and_scheduled_renewal_preserve_real_identity(self):
        command("systemctl", "--user", "show-environment")
        (ROOT / ".local").mkdir(mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="acme-deployment-", dir=ROOT / ".local"
        ) as temporary:
            root = Path(temporary)
            with authority(root) as ca:
                issuer = root / "issuer"
                initialize(issuer, ca, renew_days=7)
                bundle, first = acme.issue(issuer)
                state = root / "deployment"
                name = "chipload-acme-deploy-" + uuid.uuid4().hex[:12]
                listeners = [socket.socket(), socket.socket()]
                for listener in listeners:
                    listener.bind(("127.0.0.1", 0))
                port, admin_port = (listener.getsockname()[1] for listener in listeners)
                for listener in listeners:
                    listener.close()
                cli = [sys.executable, str(ROOT / "scripts/tls-stack"), "--state", str(state)]
                issuer_cli = [
                    sys.executable,
                    str(ROOT / "scripts/tls-acme"),
                    "--state",
                    str(issuer),
                ]
                command(
                    *cli,
                    "init",
                    "--api-host",
                    ca.hosts["api"],
                    "--identity-host",
                    ca.hosts["identity"],
                    "--port",
                    str(port),
                    "--admin-port",
                    str(admin_port),
                    "--name",
                    name,
                    "--certificates",
                    str(bundle / "certificates"),
                    "--ca-bundle",
                    str(bundle / "identity-ca.pem"),
                )
                unit_name = "chipload-acme-" + acme.identity(issuer)[:24]
                issuer_name = unit_name
                linked = False
                try:
                    command(*cli, "up")
                    values = json.loads((state / "deployment.json").read_bytes())
                    password = (state / "secrets/agent-secret").read_text()
                    original_identity = credentials.agent_identity(state, values, password)
                    command(*issuer_cli, "bind", "--deployment", str(state))
                    crash = subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            """
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'tests'))
from acme_support import acme
replace = acme.certificates.replace_file
def interrupted(*args):
    replace(*args)
    os._exit(99)
acme.certificates.replace_file = interrupted
acme.issue(Path(sys.argv[2]))
""",
                            str(ROOT),
                            str(issuer),
                        ],
                        capture_output=True,
                        timeout=180,
                    )
                    self.assertEqual(crash.returncode, 99, crash.stderr.decode())
                    self.assertTrue((state / acme.certificates.MARKER).exists())
                    record = json.loads(command("docker", "inspect", issuer_name))[0]
                    command(*issuer_cli, "run")
                    resumed = json.loads(command("docker", "inspect", issuer_name))[0]
                    self.assertEqual(record["State"]["StartedAt"], resumed["State"]["StartedAt"])
                    self.assertFalse((state / acme.certificates.MARKER).exists())
                    self.assertFalse((issuer / acme.delivery.PENDING).exists())
                    self.assertFalse((issuer / acme.MARKER).exists())
                    renewed = json.loads((issuer / "issued.json").read_bytes())
                    self.assertNotEqual(
                        first["leaves"]["api"]["sha256"], renewed["leaves"]["api"]["sha256"]
                    )
                    acme.certificates.verify_live(state, values, renewed)
                    self.assertEqual(
                        credentials.agent_identity(state, values, password), original_identity
                    )
                    original = (state / "certificates/api.crt").read_bytes()
                    (state / "certificates/api.crt").write_bytes(original + b"\n")
                    with self.assertRaisesRegex(ValueError, "outside this issuer"):
                        acme.issue(issuer)
                    (state / "certificates/api.crt").write_bytes(original)
                    self.assertEqual(
                        resumed["State"]["StartedAt"],
                        json.loads(command("docker", "inspect", issuer_name))[0]["State"][
                            "StartedAt"
                        ],
                    )
                    units = json.loads(command(*issuer_cli, "timer"))
                    service, timer = Path(units["service"]), Path(units["timer"])
                    command("systemd-analyze", "--user", "verify", str(service), str(timer))
                    # Exercise timer dispatch promptly; production uses the generated calendar.
                    timer.write_text(
                        timer.read_text()
                        .replace(
                            "OnCalendar=*-*-* 00,12:00:00 UTC", "OnActiveSec=1s\nAccuracySec=1s"
                        )
                        .replace("RandomizedDelaySec=30min", "RandomizedDelaySec=0")
                        .replace("Persistent=yes", "Persistent=no")
                    )
                    command("systemctl", "--user", "link", "--runtime", str(service), str(timer))
                    linked = True
                    command("systemctl", "--user", "daemon-reload")
                    command("systemctl", "--user", "start", unit_name + ".timer")
                    deadline = time.monotonic() + 240
                    while True:
                        status = command(
                            "systemctl",
                            "--user",
                            "show",
                            unit_name + ".service",
                            "--property=ActiveState,Result,ExecMainStatus",
                        )
                        self.assertNotIn("ActiveState=failed", status, status)
                        installed = json.loads((issuer / "issued.json").read_bytes())
                        if (
                            installed["leaves"]["api"]["sha256"]
                            != renewed["leaves"]["api"]["sha256"]
                            and "ActiveState=inactive" in status
                        ):
                            break
                        self.assertLess(time.monotonic(), deadline, status)
                        time.sleep(0.2)
                    self.assertIn("Result=success", status)
                    acme.certificates.verify_live(state, values, installed)
                    self.assertEqual(
                        credentials.agent_identity(state, values, password), original_identity
                    )
                finally:
                    if linked:
                        command(
                            "systemctl",
                            "--user",
                            "stop",
                            unit_name + ".timer",
                            unit_name + ".service",
                        )
                        command(
                            "systemctl",
                            "--user",
                            "disable",
                            "--runtime",
                            unit_name + ".timer",
                            unit_name + ".service",
                        )
                        command("systemctl", "--user", "daemon-reload")
                        subprocess.run(
                            ["systemctl", "--user", "reset-failed", unit_name + ".service"],
                            capture_output=True,
                        )
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
