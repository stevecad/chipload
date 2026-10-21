"""Credential cutovers preserve exact intent and do not silently broaden authority."""

import contextlib
import io
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from secrets_support import archive, script

workload = script.load("workload_unit", "scripts/secrets-workload")


class WorkloadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chipload-workload-unit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "service"
        self.state.mkdir(mode=0o700)
        (self.state / "certificates").mkdir(mode=0o700)
        archive.write(self.state / "certificates/ca.pem", "test-trust")
        self.credentials = self.root / "credentials"
        self.credentials.mkdir(mode=0o700)
        archive.write(self.credentials / "bao-ca.pem", "test-trust")
        self.profile = {
            "version": 1,
            "name": "evidence-adapter",
            "credentials": str(self.credentials),
            "cluster_id": "cluster",
            "secret_id_ttl": 3600,
            "generation": 2,
            "adapter": {
                "id": "evidence",
                "receiver_id": "evidence",
                "origin": "https://receiver:8443",
                "contracts": [
                    {
                        "shop_id": "document-shop",
                        "operation_id": "review",
                        "command": "review",
                        "effect": "read",
                    }
                ],
                "secrets": {
                    "origin": "https://bao:8200",
                    "ca_file": "bao-ca.pem",
                    "kv_mount": "chipload",
                    "secret_path": "shops/document-shop/evidence",
                    "policy": "evidence-adapter",
                },
            },
        }
        self.api = SimpleNamespace(
            state=self.state,
            value={"host": "bao"},
            cluster="cluster",
            call=Mock(),
            operator_token=self.root / "operator",
        )
        self.pending = {
            "version": 1,
            "id": "intent",
            "owner": workload.stack.ownership_key(self.state, self.api.value),
            "action": "rotate",
            "profile": self.profile,
            "started_at": int(time.time()),
            "next_secret_id": "replacement-credential",
            "old_secret_id": "previous-credential",
        }
        archive.write(self.credentials / "role-id", "role")
        archive.write(self.credentials / "secret-id", "previous-credential")
        workload.save_pending(self.api, self.pending)

    def test_private_paths_and_matching_ca_are_required(self):
        workload.validate_profile(self.api, self.profile)
        self.api.operator_token = self.credentials / "operator"
        with self.assertRaisesRegex(ValueError, "outside workload"):
            workload.validate_profile(self.api, self.profile, check_credentials=False)
        self.api.operator_token = self.root / "operator"
        (self.credentials / "secret-id").chmod(0o644)
        with self.assertRaises(ValueError):
            workload.credential_directory(self.state, self.profile)
        (self.credentials / "secret-id").chmod(0o600)
        (self.credentials / "bao-ca.pem").write_text("different-trust")
        with self.assertRaisesRegex(ValueError, "trust"):
            workload.credential_directory(self.state, self.profile)
        (self.credentials / "bao-ca.pem").unlink()
        (self.credentials / "bao-ca.pem").symlink_to(self.state / "certificates/ca.pem")
        with self.assertRaises((ValueError, OSError)):
            workload.credential_directory(self.state, self.profile)

    def test_policy_and_role_drift_refuse_without_writes(self):
        self.api.call.return_value = (
            200,
            {"data": {"policy": 'path "*" { capabilities = ["read"] }'}},
        )
        with self.assertRaisesRegex(ValueError, "policy changed"):
            workload.assert_config(self.api, self.profile)
        for changed in (
            "bind_secret_id",
            "token_no_default_policy",
            "token_ttl",
            "token_period",
            "token_policies",
        ):
            role = workload.role_config(self.profile)
            role[changed] = "different"
            self.api.call.reset_mock()
            self.api.call.side_effect = [
                (200, {"data": {"policy": workload.policy(self.profile)}}),
                (200, {"data": role}),
            ]
            with self.assertRaisesRegex(ValueError, "settings changed"):
                workload.assert_config(self.api, self.profile)
            self.assertTrue(all(call.args[0] == "GET" for call in self.api.call.call_args_list))

    def test_broad_login_is_revoked_before_any_secret_read(self):
        self.api.call.side_effect = [
            (
                200,
                {
                    "auth": {
                        "client_token": "overbroad",
                        "policies": ["default", "evidence-adapter"],
                        "lease_duration": 60,
                    }
                },
            ),
            (204, None),
        ]
        with self.assertRaisesRegex(ValueError, "permission"):
            workload.verify_login(self.api, self.profile, "role", "secret")
        self.assertEqual(self.api.call.call_args.args[:2], ("POST", "auth/token/revoke-self"))
        self.assertEqual(self.api.call.call_count, 2)

    def test_install_is_atomic_idempotent_and_preserves_an_unrelated_edit(self):
        path = self.credentials / "secret-id"
        old = workload.digest_secret("previous-credential")
        workload.install_file(path, "replacement-credential", old)
        workload.install_file(path, "replacement-credential", old)
        self.assertEqual(workload.read_secret(path), "replacement-credential")
        self.assertEqual(path.stat().st_mode & 0o077, 0)
        with self.assertRaisesRegex(ValueError, "outside"):
            workload.install_file(path, "unexpected", old)
        self.assertEqual(workload.read_secret(path), "replacement-credential")
        self.assertEqual(
            {p.name for p in self.credentials.iterdir()}, {"role-id", "secret-id", "bao-ca.pem"}
        )

    def test_failed_install_does_not_retire_old_credential(self):
        detail = {
            "metadata": {"chipload_operation": "intent"},
            "secret_id_accessor": "accessor",
            "expiration_time": "future",
        }
        with (
            patch.object(workload, "assert_config", return_value="role"),
            patch.object(workload, "lookup", return_value=detail),
            patch.object(workload, "verify_login", return_value=1),
            patch.object(workload, "install_file", side_effect=OSError("disk failed")),
        ):
            with self.assertRaises(OSError):
                workload.advance(self.api, self.pending)
        self.api.call.assert_not_called()
        self.assertTrue((self.state / workload.PENDING).exists())
        self.assertEqual(
            workload.read_secret(self.credentials / "secret-id"), "previous-credential"
        )

    def test_missing_issued_credential_is_not_recreated_after_expiry(self):
        self.pending["issued"] = True
        with (
            patch.object(workload, "assert_config", return_value="role"),
            patch.object(workload, "lookup", return_value=None),
        ):
            with self.assertRaisesRegex(ValueError, "expired or was removed"):
                workload.advance(self.api, self.pending)
        self.api.call.assert_not_called()

    def test_lost_receipt_completion_preserves_generation_and_secret_on_retry(self):
        detail = {
            "metadata": {"chipload_operation": "intent"},
            "secret_id_accessor": "accessor",
            "expiration_time": "future",
        }
        replace = archive.replace_json

        def lost_receipt(path, value):
            replace(path, value)
            if path.parent.name == "workloads":
                raise OSError("lost durable receipt completion")

        with (
            patch.object(workload, "assert_config", return_value="role"),
            patch.object(workload, "lookup", return_value=detail),
            patch.object(workload, "verify_login", return_value=1),
            patch.object(workload, "assert_denied_login"),
        ):
            with patch.object(archive, "replace_json", side_effect=lost_receipt):
                with self.assertRaises(OSError):
                    workload.advance(self.api, self.pending)
            result = workload.advance(self.api, script.read_json(self.state / workload.PENDING))
        self.assertEqual(result["generation"], 2)
        self.assertFalse((self.state / workload.PENDING).exists())
        receipt = (self.state / "workloads/evidence-adapter.json").read_text()
        self.assertNotIn("replacement-credential", receipt)
        self.assertNotIn("previous-credential", receipt)

    def test_emergency_revocation_does_not_depend_on_local_credential_files(self):
        self.pending["action"] = "revoke"
        (self.credentials / "secret-id").unlink()
        (self.credentials / "role-id").unlink()
        self.api.call.side_effect = [
            (204, None),
            (200, {"data": {"policy": workload.REVOKED_POLICY}}),
            (204, None),
            (404, None),
        ]
        result = workload.advance(self.api, self.pending)
        self.assertEqual(result["status"], "revoked")
        self.assertEqual(
            self.api.call.call_args_list[0].args[2], {"policy": workload.REVOKED_POLICY}
        )
        self.assertFalse((self.state / workload.PENDING).exists())

    def test_cli_never_echoes_private_response_or_validation_input(self):
        with (
            patch("sys.argv", ["secrets-workload", "--operator-token", "token-file", "resume"]),
            patch.object(
                workload, "operate", side_effect=ValueError("private response secret-value")
            ),
            contextlib.redirect_stderr(io.StringIO()) as output,
        ):
            with self.assertRaises(SystemExit):
                workload.main()
        self.assertNotIn("secret-value", output.getvalue())
        self.assertIn("resume", output.getvalue())

    def test_receipt_for_another_name_cannot_redirect_revocation(self):
        (self.state / workload.PENDING).unlink()
        (self.state / "workloads").mkdir(mode=0o700)
        archive.replace_json(self.state / "workloads/another-name.json", self.profile)
        args = SimpleNamespace(
            state=self.state,
            operator_token=self.root / "operator",
            action="revoke",
            workload="another-name",
        )
        with patch.object(workload, "API", return_value=self.api):
            with self.assertRaisesRegex(ValueError, "another identity"):
                workload.operate(args)
        self.api.call.assert_not_called()
