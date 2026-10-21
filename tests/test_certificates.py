"""Validate real certificates and recover updates without silently changing identity."""

import contextlib
import io
import json
import os
import ssl
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from certificates_support import ROOT, archive
from certificates_support import certificates as script
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

from chipload.lifecycle import Repository


def expire_leaf(state, *, future=False):
    """Resign an otherwise valid leaf so OpenSSL evaluates its actual dates."""
    path = state / "certificates/api.crt"
    original = x509.load_pem_x509_certificate(path.read_bytes())
    key = serialization.load_pem_private_key(
        (state / "recovery/local-ca-key.pem").read_bytes(), password=None
    )
    now = datetime.now(timezone.utc)
    start, end = (1, 2) if future else (-2, -1)
    builder = (
        x509.CertificateBuilder()
        .subject_name(original.subject)
        .issuer_name(original.issuer)
        .public_key(original.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + timedelta(days=start))
        .not_valid_after(now + timedelta(days=end))
    )
    for extension in original.extensions:
        builder = builder.add_extension(extension.value, critical=extension.critical)
    path.write_bytes(builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM))


class CertificateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.values = {
            "version": 1,
            "profile": "local",
            "name": "certificates-test",
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
        self.candidate = self.root / "candidate"
        self.hosts = {role: self.values[role + "_host"] for role in ("api", "identity")}
        script.stack.local_certificates(self.candidate, self.hosts)
        self.stable = script.stable_state(self.state)
        self.original = script.hashes(self.state)
        self.volume = {
            "Name": "certificates-test_keycloak-data",
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
        self.probe = self.patches.enter_context(
            patch.object(
                script.stack,
                "probe",
                return_value={"issuer": "https://identity.localhost:8443/realms/chipload"},
            )
        )

    def install(self, candidate=None, **options):
        candidate = candidate or self.candidate
        script.install(
            self.state,
            certificates=candidate / "certificates",
            ca_bundle=candidate / "identity-ca.pem",
            **options,
        )

    def test_validate_before_journaling_or_changing_active_files(self):
        for suffix in ("crt", "key"):
            (self.candidate / f"certificates/api.{suffix}").write_bytes(
                (self.candidate / f"certificates/identity.{suffix}").read_bytes()
            )
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.install()
        self.assertEqual(script.hashes(self.state), self.original)
        self.assertFalse((self.state / script.MARKER).exists())
        self.assertEqual(list(self.state.glob(".certificate-update-*")), [])
        self.compose.assert_not_called()

    def test_validity_trust_encrypted_keys_and_bounded_inputs(self):
        for future in (False, True):
            expire_leaf(self.candidate, future=future)
            with self.assertRaises((ValueError, ssl.SSLCertVerificationError)):
                self.install()
        (self.candidate / "certificates/api.crt").write_bytes(
            (self.state / "certificates/api.crt").read_bytes()
        )
        (self.candidate / "certificates/api.key").write_bytes(
            (self.state / "certificates/api.key").read_bytes()
        )
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.install()
        key = serialization.load_pem_private_key(
            (self.state / "certificates/api.key").read_bytes(), password=None
        )
        (self.candidate / "certificates/api.key").write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.BestAvailableEncryption(b"fixture-password"),
            )
        )
        with self.assertRaises(TypeError):
            self.install()
        oversized = self.candidate / "certificates/api.crt"
        oversized.write_bytes(b"x" * (script.material.MAX_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "size limit"):
            self.install()
        self.assertEqual(script.hashes(self.state), self.original)
        self.compose.assert_not_called()

    def test_partial_file_update_resumes_the_durable_candidate(self):
        replace = script.replace_file

        def interrupted(path, source):
            replace(path, source)
            raise RuntimeError("power loss after one file")

        with patch.object(script, "replace_file", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.install()
        journal = script.read_journal(self.state)
        self.assertNotEqual(script.hashes(self.state), self.original)
        with self.assertRaisesRegex(ValueError, "certificate installation"):
            script.stack.require_ready(self.state)
        # Incoming files can disappear; retry uses the persisted private candidate.
        (self.candidate / "certificates/api.key").unlink()
        script.install(self.state, action="resume")
        self.assertEqual(script.hashes(self.state), journal["replacement"])
        self.assertEqual(script.stable_state(self.state), self.stable)
        self.assertFalse((self.state / script.MARKER).exists())
        self.assertFalse(script.stage_path(self.state, journal["stage_id"]).exists())
        self.assertTrue(
            any("--force-recreate" in call.args for call in self.compose.call_args_list)
        )
        self.assertEqual(
            self.compose.call_args.args[2:],
            ("up", "--detach", "--no-recreate", "--wait", "--wait-timeout", "180"),
        )
        receipt = json.loads((self.state / "certificates.json").read_bytes())
        self.assertTrue(receipt["live_verified"])
        self.assertEqual(receipt["update_id"], journal["update_id"])
        self.assertEqual(len(self.probe.call_args_list), 3)
        for call in self.probe.call_args_list:
            self.assertEqual(len(call.kwargs["certificate_sha256"]), 64)

    def test_lost_activation_reply_resumes_and_checks_live_certificates(self):
        self.compose.side_effect = subprocess.TimeoutExpired("docker", 240)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.install()
        self.assertTrue((self.state / script.MARKER).exists())
        self.compose.side_effect = None
        self.probe.side_effect = ValueError("wrong served certificate")
        with self.assertRaises(ValueError):
            script.install(self.state, action="resume")
        self.assertFalse((self.state / "certificates.json").exists())
        self.probe.side_effect = None
        script.install(self.state, action="resume")
        self.assertEqual(self.compose.call_count, 5)

    def test_interruption_after_verification_and_partial_cleanup(self):
        remove = script.remove_stage

        def interrupted(path):
            remove(path)
            raise RuntimeError("lost completion")

        with patch.object(script, "remove_stage", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.install()
        self.assertEqual(script.read_journal(self.state)["phase"], "verified")
        self.check.return_value = (False, {})
        script.install(self.state, action="resume")
        self.assertFalse((self.state / script.MARKER).exists())
        self.assertEqual(self.compose.call_count, 3)
        self.assertIn("--no-recreate", self.compose.call_args.args)

    def test_old_expired_material_can_be_repaired_and_stopped_services_stay_stopped(self):
        expire_leaf(self.state)
        self.check.return_value = (False, {})
        self.install()
        self.compose.assert_not_called()
        self.probe.assert_not_called()
        script.material.validate(self.state, self.values)
        self.assertFalse(
            json.loads((self.state / "certificates.json").read_bytes())["live_verified"]
        )

    def test_expired_certificate_archive_restores_offline_before_repair(self):
        Repository(self.state / "data/chipload.db")
        expire_leaf(self.state)
        script.stack.check_material(self.state, verify_certificates=False)
        with self.assertRaises(ValueError):
            script.stack.check_material(self.state)
        archive.write(self.state / "keycloak.dump", b"PGDMP fixture")
        plain, encrypted, key = (
            self.root / name for name in ("snapshot.tar", "snapshot.gpg", "key")
        )
        archive.pack(self.state, plain, {"backup_id": "a" * 32})
        archive.generate_key(key)
        archive.crypt(plain, encrypted, key)
        restored = self.root / "restored"

        def deployment(state, *, check, verify_certificates=True):
            self.assertTrue(check)
            script.stack.check_material(state, verify_certificates=verify_certificates)
            return [], {}, json.loads((state / "deployment.json").read_bytes())

        with (
            patch.object(script.stack, "deployment", side_effect=deployment),
            patch.object(script.stack, "choose_subnet", return_value="10.240.2.0/28"),
            patch.object(script.recovery, "records", return_value=[]),
            patch.object(script.recovery, "restore_volume"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            script.recovery.restore(restored, encrypted, key, "restored", False, archive.MAX_BYTES)
        self.assertFalse((restored / script.recovery.RESTORE_MARKER).exists())
        with self.assertRaises(ValueError):
            script.stack.check_material(restored)
        self.assertTrue(any("pg_restore" in call.args for call in self.compose.call_args_list))
        self.assertFalse(any("gateway" in call.args for call in self.compose.call_args_list))

    def test_invalid_pending_candidate_can_be_replaced_without_bypassing_drift_checks(self):
        self.compose.side_effect = RuntimeError("interrupted")
        with self.assertRaises(RuntimeError):
            self.install()
        original = script.read_journal(self.state)
        # Stage and active hashes are still authoritative even if time has passed
        # or a stage was lost. Replacement does not reuse the missing private key.
        (script.stage_path(self.state, original["stage_id"]) / "certificates/api.key").unlink()
        newer = self.root / "newer"
        script.stack.local_certificates(newer, self.hosts)
        self.compose.side_effect = None
        self.install(newer, action="replace-pending")
        self.assertEqual(script.hashes(self.state), script.hashes(newer))
        self.assertFalse(script.stage_path(self.state, original["stage_id"]).exists())
        self.assertEqual(script.stable_state(self.state), self.stable)

    def test_drift_of_identity_storage_settings_stage_and_active_files_is_rejected(self):
        self.compose.side_effect = RuntimeError("interrupted")
        with self.assertRaises(RuntimeError):
            self.install()
        self.compose.side_effect = None
        self.volume["CreatedAt"] = "replacement"
        with self.assertRaisesRegex(ValueError, "storage changed"):
            script.install(self.state, action="resume")
        self.volume["CreatedAt"] = "original"
        path = self.state / "runtime.json"
        previous = path.read_bytes()
        path.write_bytes(previous + b" ")
        with self.assertRaisesRegex(ValueError, "settings"):
            script.install(self.state, action="resume")
        path.write_bytes(previous)
        staged = script.stage_path(self.state, script.read_journal(self.state)["stage_id"])
        path = staged / "certificates/api.key"
        previous = path.read_bytes()
        path.write_bytes(previous + b" ")
        with self.assertRaisesRegex(ValueError, "staged certificate"):
            script.install(self.state, action="resume")
        path.write_bytes(previous)
        (self.state / "identity-ca.pem").write_bytes(b"unrelated")
        with self.assertRaisesRegex(ValueError, "outside this installation"):
            self.install(action="replace-pending")

    def test_conflicting_operations_and_unsafe_staging_paths_are_rejected(self):
        for conflict in script.CONFLICTS:
            marker = self.state / conflict
            marker.symlink_to(self.root / "absent")
            with self.assertRaisesRegex(ValueError, "finish deployment"):
                self.install()
            marker.unlink()
        for value in ("../another", "/tmp/elsewhere", "", None):
            with self.assertRaises(ValueError):
                script.stage_path(self.state, value)
        self.compose.assert_not_called()

    def test_status_exit_codes_expiry_and_no_private_data_or_environment_key_log(self):
        key_log = self.root / "key-log"
        with patch.dict(os.environ, {"SSLKEYLOGFILE": str(key_log)}):
            report, code = script.status(self.state, warn_days=1)
            self.assertEqual(code, 0)
            self.assertTrue(report["valid"])
            self.assertFalse(report["renewal_due"])
            report, code = script.status(self.state, warn_days=14)
        self.assertEqual(code, 2)
        self.assertTrue(report["renewal_due"])
        self.assertFalse(key_log.exists())
        self.assertNotIn("PRIVATE KEY", json.dumps(report))
        expire_leaf(self.state)
        report, code = script.status(self.state)
        self.assertEqual(code, 1)
        self.assertFalse(report["valid"])
        # The actual status CLI requires no Docker connection and is suitable for monitoring.
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/tls-certificates"),
                "--state",
                str(self.state),
                "status",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "DOCKER_HOST": "invalid"},
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(json.loads(result.stdout)["valid"])

    def test_credential_resume_and_backup_refuse_pending_certificates(self):
        from credentials_support import credentials

        archive.write(self.state / script.MARKER, "{}")
        with self.assertRaisesRegex(ValueError, "certificate"):
            script.stack.require_ready(self.state)
        with self.assertRaisesRegex(credentials.CredentialError, "certificate"):
            credentials.rotate(self.state, "resume", resume=True)
        with patch.object(archive, "check_key"):
            with self.assertRaisesRegex(ValueError, "certificate"):
                script.recovery.backup(
                    self.state, self.root / "backup", self.root / "key", archive.MAX_BYTES
                )
