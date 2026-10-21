"""Offline checks for local bootstrap credential handling and recovery."""

import importlib.machinery
import importlib.util
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from secrets_environment_support import ROOT

loader = importlib.machinery.SourceFileLoader(
    "dev_stack", str(ROOT / "scripts/dev-stack")
)
spec = importlib.util.spec_from_loader(loader.name, loader)
stack = importlib.util.module_from_spec(spec)
loader.exec_module(stack)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / ".local/dev-stack"
        policy = self.root / "deploy/openbao/chipload-example.hcl"
        policy.parent.mkdir(parents=True)
        policy.write_text('path "chipload/data/shops/example/*" { capabilities = ["read"] }')
        for attribute, value in (("ROOT", self.root), ("STATE", self.state)):
            patcher = patch.object(stack, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_generated_env_is_private_random_and_preserved(self):
        stack.ensure_env()
        path = self.root / ".env"
        original = path.read_text()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        values = dict(line.split("=", 1) for line in original.splitlines() if line and not line.startswith("#"))
        self.assertGreaterEqual(len(values["KEYCLOAK_ADMIN_PASSWORD"]), 40)
        self.assertNotEqual(values["KEYCLOAK_ADMIN_PASSWORD"], values["KEYCLOAK_DB_PASSWORD"])
        stack.ensure_env()
        self.assertEqual(path.read_text(), original)

    def test_private_write_does_not_overwrite_existing_recovery_file(self):
        path = self.state / "secret.json"
        stack.private_write(path, "original")
        with self.assertRaises(FileExistsError):
            stack.private_write(path, "replacement")
        self.assertEqual(path.read_text(), "original")
        stack.private_write(path, "rotated", replace=True)
        self.assertEqual(path.read_text(), "rotated")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_initialized_storage_without_keys_is_not_modified(self):
        with patch.object(stack, "bao_request", return_value={"initialized": True}) as request:
            with self.assertRaisesRegex(stack.StackError, "already initialized"):
                stack.bootstrap("http://127.0.0.1:8200")
        self.assertEqual(request.call_count, 1)

    def test_empty_storage_with_old_keys_is_not_reinitialized(self):
        stack.private_write(self.state / "openbao-init.json", "old recovery material")
        with patch.object(stack, "bao_request", return_value={"initialized": False}) as request:
            with self.assertRaisesRegex(stack.StackError, "old bootstrap keys"):
                stack.bootstrap("http://127.0.0.1:8200")
        self.assertEqual(request.call_count, 1)

    def test_bootstrap_resumes_and_rotates_workload_identity_without_reinitializing(self):
        state = {"initialized": False, "sealed": True, "issued": 0}
        calls = []

        def request(base, method, path, data=None, *, token=None):
            calls.append((method, path, data, token))
            if path == "sys/init":
                if method == "GET":
                    return {"initialized": state["initialized"]}
                state["initialized"] = True
                return {"root_token": "test-admin", "keys_base64": ["test-unseal"]}
            if path == "sys/seal-status":
                return {"sealed": state["sealed"]}
            if path == "sys/unseal":
                state["sealed"] = False
                return {"sealed": False}
            if path == "sys/audit":
                return {"data": {"file/": {"type": "file"}}}
            if path in ("sys/mounts", "sys/auth"):
                return {"data": {}}
            if path.endswith("/role-id"):
                return {"data": {"role_id": "test-role"}}
            if path.endswith("/secret-id"):
                state["issued"] += 1
                return {"data": {"secret_id": f"test-secret-{state['issued']}", "secret_id_accessor": f"test-accessor-{state['issued']}"}}

        with patch.object(stack, "bao_request", side_effect=request):
            stack.bootstrap("http://127.0.0.1:8200")
            first = (self.state / "openbao-init.json").read_text()
            state["sealed"] = True  # Simulate a restart of persistent storage.
            stack.bootstrap("http://127.0.0.1:8200")
        self.assertEqual((self.state / "openbao-init.json").read_text(), first)
        self.assertEqual(sum(method == "PUT" and path == "sys/init" for method, path, _, _ in calls), 1)
        self.assertEqual(sum(path == "sys/unseal" for _, path, _, _ in calls), 2)
        workload = json.loads((self.state / "runtime-approle.json").read_text())
        self.assertEqual(workload["secret_id"], "test-secret-2")
        self.assertNotIn("test-admin", json.dumps(workload))
        self.assertIn(("POST", "auth/approle/role/chipload-example/secret-id-accessor/destroy", {"secret_id_accessor": "test-accessor-1"}, "test-admin"), calls)


if __name__ == "__main__":
    unittest.main()
