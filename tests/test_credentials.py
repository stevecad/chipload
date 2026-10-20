"""Credential cutovers retain a durable retry intent and never expose passwords."""

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from credentials_support import archive
from credentials_support import credentials as script


class CredentialTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.values = {
            "version": 1,
            "profile": "local",
            "name": "credential-test",
            "api_host": "chipload.localhost",
            "identity_host": "identity.localhost",
            "port": 8443,
            "admin_port": 8444,
            "subnet": "10.240.1.0/28",
            "shop_id": "example",
        }
        args = SimpleNamespace(
            **self.values,
            state=self.state,
            local=True,
            shop="example",
            certificates=None,
            ca_bundle=None,
        )
        with (
            patch.object(script.stack.subprocess, "check_output", return_value=""),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            script.stack.initialize(args)
        self.old = (self.state / "secrets/agent-secret").read_text()
        self.remote = self.old
        self.identity = {"subject": "fixture-subject", "grants": [{"shop_id": "example"}]}
        self.volume = {
            "Name": "credential-test_keycloak-data",
            "CreatedAt": "original",
            "Driver": "local",
            "Labels": {},
        }
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(
            patch.object(script.stack, "deployment", return_value=([], {}, self.values))
        )
        self.check = self.patches.enter_context(
            patch.object(script.recovery, "check_containers", return_value=(True, {}))
        )
        self.patches.enter_context(
            patch.object(script.recovery, "records", side_effect=lambda *a: [self.volume])
        )
        self.compose = self.patches.enter_context(patch.object(script.recovery, "compose"))
        self.login = self.patches.enter_context(
            patch.object(script, "operator_token", return_value="operator-token")
        )
        self.patches.enter_context(
            patch.object(script, "agent_identity", return_value=self.identity)
        )
        self.patches.enter_context(
            patch.object(
                script, "client_path", return_value="/admin/realms/chipload/clients/fixture"
            )
        )
        self.patches.enter_context(patch.object(script, "verify_subject"))
        self.patches.enter_context(
            patch.object(script, "current_agent", side_effect=lambda *a: self.remote)
        )
        self.patches.enter_context(
            patch.object(
                script,
                "agent_token",
                side_effect=lambda s, v, password: (
                    (200, {}) if password == self.remote else (401, {"error": "invalid_client"})
                ),
            )
        )

    def pending_agent(self):
        def lost_response(*args, **kwargs):
            if kwargs.get("method", "GET") == "GET":
                return 200, {"clientId": "chipload-agent", "authorizationServicesEnabled": False}
            self.remote = kwargs["data"]["secret"]
            raise OSError("connection lost after identity commit")

        with patch.object(script, "request", side_effect=lost_response):
            with self.assertRaises(OSError):
                script.rotate(self.state, "agent")
        return json.loads((self.state / script.MARKER).read_bytes())

    def test_lost_identity_commit_response_resumes_the_same_secret_and_preserves_grants(self):
        before = script.stable_state(self.state)
        journal = self.pending_agent()
        self.assertTrue(self.remote != self.old)
        self.assertTrue((self.state / "secrets/agent-secret").read_text() == self.old)
        self.assertEqual((self.state / script.MARKER).stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(ValueError, "rotation is incomplete"):
            script.stack.require_ready(self.state)
        with patch.object(script, "request", return_value=(204, None)) as request:
            script.rotate(self.state, "resume", resume=True)
        self.assertEqual(request.call_args.kwargs["method"], "DELETE")
        self.assertEqual(request.call_count, 1)  # The committed PUT was not repeated.
        self.assertTrue((self.state / "secrets/agent-secret").read_text() == journal["new"])
        self.assertTrue(script.realm_record(self.state)[1]["secret"] == journal["new"])
        self.assertEqual(script.stable_state(self.state), before)
        self.assertFalse((self.state / script.MARKER).exists())
        receipt = (self.state / "credentials.json").read_text()
        self.assertNotIn(journal["old"], receipt)
        self.assertNotIn(journal["new"], receipt)

    def test_resume_rejects_changed_bindings_storage_state_path_and_external_secret(self):
        journal = self.pending_agent()
        runtime = self.state / "runtime.json"
        original = runtime.read_bytes()
        runtime.write_bytes(original + b" ")
        with self.assertRaisesRegex(ValueError, "settings or identity storage changed"):
            script.rotate(self.state, "resume", resume=True)
        runtime.write_bytes(original)
        self.volume["CreatedAt"] = "replacement"
        with self.assertRaisesRegex(ValueError, "settings or identity storage changed"):
            script.rotate(self.state, "resume", resume=True)
        self.volume["CreatedAt"] = "original"
        self.remote = "x" * 43
        with self.assertRaisesRegex(ValueError, "changed outside"):
            script.rotate(self.state, "resume", resume=True)
        archive.replace_json(self.state / script.MARKER, {**journal, "state": "/another/state"})
        with self.assertRaisesRegex(ValueError, "different state directory"):
            script.rotate(self.state, "resume", resume=True)

    def test_database_commit_interruption_reuses_password_and_recreates_secret_mounts(self):
        self.remote = (self.state / "secrets/db-password").read_text()
        old = self.remote

        def changed_then_interrupted(command, environment, password):
            self.remote = password
            raise OSError("connection lost after database commit")

        with patch.object(script, "database_accepts", side_effect=lambda c, e, p: p == self.remote):
            with patch.object(
                script, "set_database_password", side_effect=changed_then_interrupted
            ):
                with self.assertRaises(OSError):
                    script.rotate(self.state, "database")
            journal = json.loads((self.state / script.MARKER).read_bytes())
            self.assertTrue(self.remote != old)
            with patch.object(script, "set_database_password") as change:
                script.rotate(self.state, "resume", resume=True)
                change.assert_not_called()
        self.assertTrue((self.state / "secrets/db-password").read_text() == journal["new"])
        self.assertEqual((self.state / "secrets/db-password").stat().st_mode & 0o777, 0o444)
        self.assertIn("--force-recreate", self.compose.call_args_list[-1].args)
        self.assertFalse((self.state / script.MARKER).exists())
        self.login.assert_not_called()

    def test_wrong_operator_or_unowned_containers_never_create_pending_changes(self):
        self.login.side_effect = ValueError("operator authentication failed")
        with self.assertRaises(ValueError):
            script.rotate(self.state, "agent")
        self.assertFalse((self.state / script.MARKER).exists())
        self.login.reset_mock(side_effect=True)
        self.check.side_effect = ValueError("containers belong to another state directory")
        with self.assertRaisesRegex(ValueError, "another state directory"):
            script.rotate(self.state, "database")
        self.assertFalse((self.state / script.MARKER).exists())
        self.compose.assert_not_called()

    def test_database_authentication_failure_is_distinct_from_connection_failure(self):
        password = "a" * 43
        result = subprocess.CompletedProcess([], 0, stdout=b"keycloak\n", stderr=b"")
        with patch.object(script.subprocess, "run", return_value=result) as run:
            self.assertTrue(script.database_accepts([], {}, password))
            self.assertNotIn(password, repr(run.call_args.args))
            self.assertNotIn(password, repr(run.call_args.kwargs["env"]))
            self.assertEqual(run.call_args.kwargs["input"], (password + "\n").encode())
            self.assertIn("--host=keycloak-db", run.call_args.args[0])
            result.returncode = 2
            result.stderr = b'FATAL: password authentication failed for user "keycloak"'
            self.assertFalse(script.database_accepts([], {}, password))
            result.stderr = b"connection refused"
            with self.assertRaisesRegex(ValueError, "other than authentication"):
                script.database_accepts([], {}, password)

    def test_database_password_change_uses_psql_encryption_and_stdin_only(self):
        password = "b" * 43
        result = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
        with patch.object(script.subprocess, "run", return_value=result) as run:
            script.set_database_password([], {}, password)
            self.assertNotIn(password, repr(run.call_args.args))
            self.assertIn("--command=\\password keycloak", run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs["input"], ((password + "\n") * 2).encode())

    def test_cli_does_not_print_secrets_from_dependency_errors(self):
        with (
            patch.object(script, "rotate", side_effect=ValueError(self.old)),
            patch("sys.argv", ["tls-credentials", "rotate-agent"]),
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            with self.assertRaises(SystemExit):
                script.main()
        self.assertNotIn(self.old, errors.getvalue())
        self.assertIn("resume", errors.getvalue())

    def test_wrong_password_authentication_requires_an_explicit_client_error(self):
        for error in ("invalid_client", "unauthorized_client"):
            script.rejected(401, {"error": error})
        for status, result in (
            (200, {}),
            (503, {"error": "invalid_client"}),
            (401, {"error": "access_denied"}),
            (401, None),
        ):
            with self.subTest(status=status, result=result), self.assertRaises(ValueError):
                script.rejected(status, result)

    def test_operator_token_files_require_private_regular_files_and_are_not_sent_for_login(self):
        path = self.root / "operator-token"
        archive.write(path, "operator-access-token\n")
        # Test the actual file reader, bypassing setUp's fake operator login.
        self.patches.close()
        with patch.object(script, "request") as request:
            self.assertEqual(
                script.operator_token(
                    self.state, self.values, "unused", self.root / "absent", path
                ),
                "operator-access-token",
            )
            request.assert_not_called()
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            script.operator_token(self.state, self.values, "unused", self.root / "absent", path)
        path.chmod(0o600)
        path.write_text("one\ntwo")
        with self.assertRaises(ValueError):
            script.operator_token(self.state, self.values, "unused", self.root / "absent", path)

    def test_unfinished_marker_symlinks_and_competing_operations_block_rotation(self):
        marker = self.state / script.MARKER
        marker.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(ValueError, "rotation is incomplete"):
            script.rotate(self.state, "database")
        marker.unlink()
        with script.stack.operation_lock(self.state):
            with self.assertRaisesRegex(ValueError, "another deployment"):
                script.rotate(self.state, "database")
        self.compose.assert_not_called()

    def test_database_that_accepts_any_password_is_rejected_before_maintenance(self):
        with patch.object(script, "database_accepts", return_value=True):
            with self.assertRaisesRegex(ValueError, "unknown password"):
                script.rotate(self.state, "database")
        self.assertFalse((self.state / script.MARKER).exists())
        self.compose.assert_not_called()
