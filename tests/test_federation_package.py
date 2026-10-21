"""Private launcher inputs, TLS/credential boundaries and initialization refusal."""

import importlib.machinery
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tls_support import stack

from chipload.auth_settings import ServerSettings
from chipload.cloud import initialize_cloud
from chipload.connector import CloudLink, ConnectorSettings
from chipload.connector_auth import WorkloadIdentity
from chipload.directory_model import CloudSettings
from chipload.federation_wire import EgressPolicy

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("federation_script", str(ROOT / "scripts/federation"))
script = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(script)


class FederationPackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tls = self.root / "tls"
        stack.local_certificates(self.tls, {"api": "cloud.example.test", "identity": "identity.example.test"})
        self.credentials = self.root / "credentials"
        self.credentials.mkdir(mode=0o700)
        for name, value in (("role-id", b"fixture-role"), ("secret-id", b"fixture-secret"),
                            ("ca.pem", (self.tls / "identity-ca.pem").read_bytes())):
            script.write(self.credentials / name, value)
        self.trust = ServerSettings(resource_url="https://cloud.example.test:9443/mcp",
            issuer="https://identity.example.test/realms/chipload", jwks_url="https://identity.example.test/certs")
        self.identity = WorkloadIdentity(client_id="connector", subject="owner", trust=self.trust,
            secrets={"origin": "https://bao.example.test", "ca_file": "ca.pem", "kv_mount": "connector", "secret_path": "cloud/client", "policy": "connector"})
        self.config = ConnectorSettings(instance_id="a" * 32, source_origin="https://shop.example.test",
            source_policy=EgressPolicy(ca_file=self.tls / "identity-ca.pem"), shops=("shop",),
            clouds=(CloudLink(id="cloud", origin="https://cloud.example.test:9443", directory_id="b" * 32,
                             credentials=self.credentials, identity=self.identity),))
        self.input = self.root / "input.json"
        script.write(self.input, self.config.model_dump_json().encode())
        self.state = self.root / "state"
        self.patches = [patch.object(script.runtime, "image", return_value="sha256:" + "c" * 64),
                        patch.object(script.runtime, "check_images"), patch.object(script.stack, "check_tls_images"),
                        patch.object(script.stack, "choose_subnet", return_value="10.250.255.0/28")]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_connector_mounts_only_configured_readonly_credentials_and_ca(self):
        values = script.prepare(self.state, "connector", self.input, name="fixture-connector", networks=("own_application",))
        self.assertEqual(values["role"], "connector")
        self.assertNotIn("fixture-secret", json.dumps(values))
        normalized = ConnectorSettings.model_validate_json((self.state / "settings.json").read_bytes())
        self.assertTrue(str(normalized.clouds[0].credentials).startswith("/run/chipload/credentials/"))
        with patch.object(script.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
            script.compose(self.state, values, "config")
        override = json.loads((self.state / "compose.json").read_bytes())
        service = override["services"]["connector"]
        self.assertTrue(all(mount["read_only"] for mount in service["volumes"]))
        self.assertFalse(any(mount["target"] in {"/data", "/var/run/docker.sock"} for mount in service["volumes"]))
        self.assertEqual(override["networks"]["external0"]["name"], "own_application")
        (self.credentials / "secret-id").unlink()
        script.load(self.state, check=False)  # Stopping must not require a working credential.
        with self.assertRaises(ValueError):
            script.load(self.state)

    def test_unrelated_credentials_and_symlinks_are_not_mounted(self):
        script.write(self.credentials / "root-token", b"fixture-unrelated-root")
        with self.assertRaises((ValueError, OSError)):
            script.prepare(self.state, "connector", self.input, name="fixture")
        self.assertFalse(self.state.exists())
        (self.credentials / "root-token").unlink()
        (self.credentials / "role-id").unlink()
        (self.credentials / "role-id").symlink_to(self.credentials / "secret-id")
        with self.assertRaises((ValueError, OSError)):
            script.prepare(self.state, "connector", self.input, name="fixture")

    def cloud(self):
        value = CloudSettings(directory_id="b" * 32, identity=self.trust.model_copy(update={
            "database": self.root / "ignored.db", "jwks_ca_file": self.tls / "identity-ca.pem"}), bindings=())
        self.input.unlink()
        script.write(self.input, value.model_dump_json().encode())
        return value

    def test_cloud_keys_are_only_mounted_in_the_frontend_and_data_stays_private(self):
        self.cloud()
        values = script.prepare(self.state, "cloud", self.input, name="fixture-cloud", certificates=self.tls)
        with patch.object(script.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
            script.compose(self.state, values, "config")
        services = json.loads((self.state / "compose.json").read_bytes())["services"]
        backend = services["cloud"]["volumes"]
        frontend = services["gateway"]["volumes"]
        self.assertTrue(any(item["target"] == "/data" and not item.get("read_only", False) for item in backend))
        self.assertFalse(any(item["source"].endswith("api.key") for item in backend))
        self.assertTrue(any(item["source"].endswith("api.key") for item in frontend))
        self.assertEqual(values["bind"], "127.0.0.1")
        self.assertFalse((self.state / "data/cloud.db").exists())
        with self.assertRaises(FileNotFoundError):
            script.cloud_ready(self.state)
        self.assertFalse((self.state / "data/cloud.db").exists())

    def test_cloud_verification_never_recreates_missing_accounting(self):
        value = self.cloud()
        script.prepare(self.state, "cloud", self.input, name="fixture-cloud", certificates=self.tls)
        actual = value.model_copy(update={"identity": value.identity.model_copy(update={"database": self.state / "data/cloud.db"})})
        initialize_cloud(actual)
        script.cloud_ready(self.state)
        (self.state / "data/cloud.db.limits.admission").unlink()
        with self.assertRaises(FileNotFoundError):
            script.cloud_ready(self.state)
        self.assertFalse((self.state / "data/cloud.db.limits.admission").exists())

    def test_changed_config_and_mutable_image_references_are_refused(self):
        script.prepare(self.state, "connector", self.input, name="fixture")
        with (self.state / "settings.json").open("a") as output:
            output.write(" ")
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            script.load(self.state)
        value = json.loads((self.state / "deployment.json").read_bytes())
        value["images"]["connector"] = "chipload:latest"
        (self.state / "deployment.json").write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "immutable"):
            script.load(self.state, check=False)
