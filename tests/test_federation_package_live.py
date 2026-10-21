"""Actual cloud containers, verified frontend TLS and persistent private accounting."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tls_support import ROOT, stack

from chipload.auth_settings import ServerSettings
from chipload.client import ServiceError
from chipload.cloud_client import CloudClient
from chipload.directory_model import CloudSettings, SupplierSearch
from chipload.federation_wire import EgressPolicy, request
from examples.federation_shops import term


@unittest.skipUnless(os.environ.get("CHIPLOAD_FEDERATION_TESTS") == "1", "requires built cloud, connector and TLS images")
class FederationPackageLiveTests(unittest.TestCase):
    def test_cloud_tls_container_boundaries_restart_and_missing_accounting_refusal(self):
        with tempfile.TemporaryDirectory(prefix="chipload-cloud-package-") as temporary:
            root = Path(temporary)
            tls, state = root / "tls", root / "state"
            stack.local_certificates(tls, {"api": "localhost", "identity": "localhost"})
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            listener.close()
            origin = f"https://localhost:{port}"
            config = CloudSettings(directory_id="b" * 32, identity=ServerSettings(database=root / "unused.db",
                resource_url=origin + "/mcp", issuer="https://identity.example.test/realms/chipload",
                jwks_url="https://identity.example.test/certs"), bindings=())
            config_path = root / "config.json"
            config_path.touch(mode=0o600)
            config_path.write_text(config.model_dump_json())
            name = "chipload-cloud-package-" + root.name.removeprefix("chipload-cloud-package-")
            command = [sys.executable, str(ROOT / "scripts/federation"), "--state", str(state)]

            def run(action, *arguments, expected=0):
                result = subprocess.run([*command, action, *arguments], capture_output=True, text=True, timeout=240)
                self.assertEqual(result.returncode, expected, result.stderr)
                return result.stdout

            run("prepare", "--role", "cloud", "--config", str(config_path), "--name", name, "--certificates", str(tls))
            try:
                run("up", expected=1)
                self.assertFalse((state / "data/cloud.db").exists())
                run("initialize")
                run("initialize", expected=1)
                marker = state / ".initialization-attempted"
                marker.touch(mode=0o600)
                run("up", expected=1)
                run("verify-initialization")
                run("up")
                client = CloudClient(origin, config.directory_id, policy=EgressPolicy(private_origins=(origin,),
                    ca_file=tls / "identity-ca.pem", seconds=5))
                self.assertEqual(client.descriptor().directory_id, config.directory_id)
                self.assertEqual(client.search(SupplierSearch(profile=term("material-supply"))).candidates, ())
                with self.assertRaises(ServiceError) as denied:
                    request(origin, "GET", "/v1/instances/" + "a" * 32 + "/publications/shop", policy=client.policy)
                self.assertEqual(denied.exception.status, 401)
                ids = subprocess.check_output(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={name}"], text=True).split()
                records = json.loads(subprocess.check_output(["docker", "inspect", *ids], text=True))
                self.assertEqual(len(records), 2)
                for record in records:
                    role = record["Config"]["Labels"]["com.docker.compose.service"]
                    self.assertEqual(record["Config"]["User"], f"{os.getuid()}:{os.getgid()}")
                    self.assertTrue(record["HostConfig"]["ReadonlyRootfs"])
                    self.assertEqual(record["HostConfig"]["CapDrop"], ["ALL"])
                    self.assertFalse(any(m["Destination"] == "/var/run/docker.sock" for m in record["Mounts"]))
                    if role == "cloud":
                        self.assertFalse(record["HostConfig"]["PortBindings"])
                        self.assertTrue(any(m["Destination"] == "/data" and m["RW"] for m in record["Mounts"]))
                        self.assertFalse(any(m["Source"].endswith(".key") for m in record["Mounts"]))
                    else:
                        self.assertTrue(any(m["Destination"] == "/tls/api.key" and not m["RW"] for m in record["Mounts"]))
                        self.assertEqual(record["HostConfig"]["PortBindings"]["8443/tcp"][0]["HostIp"], "127.0.0.1")
                before = (state / "data/cloud.db.limits.admission").stat().st_ino
                run("down")
                run("up")
                self.assertEqual(client.descriptor().directory_id, config.directory_id)
                self.assertEqual((state / "data/cloud.db.limits.admission").stat().st_ino, before)
                run("down")
                (state / "data/cloud.db.limits.admission").unlink()
                run("up", expected=1)
                self.assertFalse((state / "data/cloud.db.limits.admission").exists())
            finally:
                run("down")
