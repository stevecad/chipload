"""Issuer recovery authenticates bounded snapshots without replacing local state."""

import copy
import io
import json
import os
import shutil
import struct
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from acme_recovery_support import acme, archive, bundle, recovery
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

stopped_issuer = recovery.stopped
check_storage_users = recovery.storage_users


class ACMERecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "issuer"
        provider = self.root / "provider"
        self.hosts = {"api": "shop.example.test", "identity": "identity.example.test"}
        acme.stack.local_certificates(provider, self.hosts)
        acme.initialize(
            SimpleNamespace(
                state=self.state,
                api_host=self.hosts["api"],
                identity_host=self.hosts["identity"],
                server="https://acme.example.test/dir",
                email="operator@example.test",
                listen_address="127.0.0.1",
                http_port=8080,
                network="bridge",
                renew_days=0,
                accept_terms=True,
                ca_bundle=provider / "identity-ca.pem",
                directory_ca=provider / "identity-ca.pem",
            )
        )
        self.values = acme.load(self.state)
        self.leaf = x509.load_pem_x509_certificate((provider / "certificates/api.crt").read_bytes())
        self.signing = serialization.load_pem_private_key(
            (provider / "recovery/local-ca-key.pem").read_bytes(), password=None
        )
        self.key_bytes = (provider / "certificates/api.key").read_bytes()
        self.client = self.state / "client"
        archive.write(self.client / "accounts/server/shop/shop.key", self.key_bytes)
        archive.write(
            self.client / "accounts/server/shop/account.json",
            json.dumps({"Registration": {"uri": "https://acme.example.test/acct/123"}}),
        )
        archive.write(self.client / "certificates/shop.key", self.key_bytes)
        archive.write(self.client / "certificates/shop.crt", self.certificate())
        acme.private_client(self.state)
        acme.check_account(self.state, record=True)
        acme.export_bundle(self.state, self.values)
        archive.replace_json(
            self.state / "binding.json",
            {
                "version": 1,
                "deployment": str(self.root / "site"),
                "material": {},
                "configuration": "old",
            },
        )
        archive.write(self.state / "systemd/old.service", "[Service]\nExecStart=/old/path\n")
        self.key, self.output = self.root / "key", self.root / "backup.gpg"
        archive.generate_key(self.key)
        self.args = SimpleNamespace(state=self.state, key=self.key, archive=self.output)
        for target, name, value in (
            (recovery, "stopped", None),
            (recovery, "storage_users", None),
            (acme, "inspect", None),
            (acme.stack.runtime, "docker_socket", Path("/run/docker.sock")),
        ):
            mock = patch.object(target, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def certificate(self, *, expired=False):
        now = datetime.now(timezone.utc)
        builder = (
            x509.CertificateBuilder()
            .subject_name(self.leaf.subject)
            .issuer_name(self.leaf.issuer)
            .public_key(self.leaf.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(
                now - timedelta(days=10) if expired else self.leaf.not_valid_before_utc
            )
            .not_valid_after(now - timedelta(days=1) if expired else self.leaf.not_valid_after_utc)
        )
        for extension in self.leaf.extensions:
            value = (
                x509.SubjectAlternativeName([x509.DNSName(host) for host in self.hosts.values()])
                if isinstance(extension.value, x509.SubjectAlternativeName)
                else extension.value
            )
            builder = builder.add_extension(value, extension.critical)
        return builder.sign(self.signing, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)

    def test_snapshot_round_trip_preserves_keys_and_registration_without_old_units(self):
        target = self.root / "snapshot.bin"
        manifest = bundle.pack(self.state, target, acme)
        restored = self.root / "checked"
        self.assertEqual(bundle.unpack(target, restored, acme), manifest)
        for name in manifest["files"]:
            self.assertEqual((self.state / name).read_bytes(), (restored / name).read_bytes())
            self.assertEqual((restored / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(os.readlink(restored / "current"), os.readlink(self.state / "current"))
        self.assertFalse((restored / "systemd").exists())
        self.assertFalse((restored / ".operation.lock").exists())

    def test_pending_runs_links_hardlinks_and_changed_account_are_refused(self):
        for marker in (acme.MARKER, acme.delivery.PENDING):
            archive.write(self.state / marker, "{}")
            with self.assertRaisesRegex(ValueError, "pending issuance"):
                bundle.snapshot(self.state, acme)
            (self.state / marker).unlink()
        link = self.state / "client/outside"
        link.symlink_to(self.key)
        with self.assertRaises(ValueError):
            bundle.snapshot(self.state, acme)
        link.unlink()
        os.link(self.key, link)
        with self.assertRaisesRegex(ValueError, "hard link"):
            bundle.snapshot(self.state, acme)
        link.unlink()
        key = next((self.client / "accounts").rglob("*.key"))
        key.unlink()
        with self.assertRaisesRegex(ValueError, "key storage changed"):
            bundle.pack(self.state, self.root / "invalid.bin", acme)

    def test_malformed_inventory_never_writes_outside_preparation(self):
        source = self.root / "snapshot.bin"
        original = bundle.pack(self.state, source, acme)
        for changes in (
            {"files": {**original["files"], "../outside": {"size": 0, "sha256": "0" * 64}}},
            {"files": {**original["files"], "/absolute": {"size": 0, "sha256": "0" * 64}}},
            {"files": {**original["files"], "client": {"size": 0, "sha256": "0" * 64}}},
            {"files": {**original["files"], "client/accounts": {"size": 0, "sha256": "0" * 64}}},
            {"current": "../../outside"},
            {"image": "unreviewed"},
            {"format": True},
            {
                "files": {
                    **original["files"],
                    "client/huge": {"size": bundle.MAX_FILE + 1, "sha256": "0" * 64},
                }
            },
        ):
            manifest = {**copy.deepcopy(original), **changes}
            payload = json.dumps(manifest).encode()
            with self.subTest(changes=changes.keys()), self.assertRaises((ValueError, TypeError)):
                bundle.header(
                    io.BytesIO(bundle.MAGIC + struct.pack(">I", len(payload)) + payload), acme
                )
        self.assertFalse((self.root / "outside").exists())
        raw = source.read_bytes()
        for index, content in enumerate(
            (raw[:-1], raw + b"trailing", raw[:-1] + bytes([raw[-1] ^ 1]))
        ):
            corrupt = self.root / f"corrupt-{index}"
            archive.write(corrupt, content)
            with self.assertRaisesRegex(ValueError, "truncated|changed|trailing"):
                bundle.unpack(corrupt, self.root / f"rejected-{index}", acme)

    def test_changed_source_bytes_do_not_produce_a_valid_snapshot(self):
        original = bundle.validate

        def changed(*arguments):
            value = original(*arguments)
            (self.state / "client.json").write_text("{}")
            return value

        with patch.object(bundle, "validate", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "changed during backup"):
                bundle.pack(self.state, self.root / "changed.bin", acme)

    def test_expired_leaf_can_be_recovered_for_renewal(self):
        authority = acme.material.certificates(self.state / "identity-ca.pem")[0]
        now = datetime.now(timezone.utc)
        builder = (
            x509.CertificateBuilder()
            .subject_name(authority.subject)
            .issuer_name(authority.issuer)
            .public_key(authority.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=30))
            .not_valid_after(now + timedelta(days=30))
        )
        for extension in authority.extensions:
            builder = builder.add_extension(extension.value, extension.critical)
        ca_pem = builder.sign(self.signing, hashes.SHA256()).public_bytes(
            serialization.Encoding.PEM
        )
        certificate = self.certificate(expired=True)
        fingerprint = x509.load_pem_x509_certificate(certificate).fingerprint(hashes.SHA256()).hex()
        previous = (self.state / "current").resolve()
        restored_bundle = self.state / "bundles" / fingerprint
        shutil.copytree(previous, restored_bundle)
        (self.state / "identity-ca.pem").write_bytes(ca_pem)
        (restored_bundle / "identity-ca.pem").write_bytes(ca_pem)
        for role in ("api", "identity"):
            (restored_bundle / f"certificates/{role}.crt").write_bytes(certificate)
        (self.client / "certificates/shop.crt").write_bytes(certificate)
        (self.state / "current").unlink()
        (self.state / "current").symlink_to("bundles/" + fingerprint)
        archive.replace_json(
            self.state / "issued.json",
            {"version": 1, **acme.material.inspect(restored_bundle, self.values)},
        )
        source = self.root / "expired.bin"
        bundle.pack(self.state, source, acme)
        bundle.unpack(source, self.root / "restored-expired", acme)
        with self.assertRaisesRegex(ValueError, "remaining validity"):
            acme.material.validate(restored_bundle, self.values)

    def test_registered_account_can_be_saved_before_first_successful_certificate(self):
        (self.state / "issued.json").unlink()
        (self.state / "current").unlink()
        shutil.rmtree(self.client / "certificates")
        shutil.rmtree(self.state / "bundles")
        (self.state / "bundles").mkdir(mode=0o700)
        source = self.root / "account-only.bin"
        bundle.pack(self.state, source, acme)
        target = self.root / "account-only"
        manifest = bundle.unpack(source, target, acme)
        self.assertIsNone(manifest["current"])
        self.assertEqual(acme.account_keys(target), acme.account_keys(self.state))

    @unittest.skipUnless(shutil.which("gpg"), "requires GnuPG")
    def test_encrypted_backup_lost_reply_and_restore_leave_delivery_disabled(self):
        before = bundle.snapshot(self.state, acme)
        publish = recovery.publish

        def lost(*arguments):
            publish(*arguments)
            raise RuntimeError("lost backup reply")

        with patch.object(recovery, "publish", side_effect=lost), self.assertRaises(RuntimeError):
            recovery.backup(self.args)
        self.assertEqual(before, bundle.snapshot(self.state, acme))
        inspected = recovery.inspect_archive(self.args)
        self.assertEqual(inspected["source"], str(self.state))
        self.assertEqual(
            inspected["source_container"], "chipload-acme-" + acme.identity(self.state)[:24]
        )
        with self.assertRaisesRegex(ValueError, "already exists"):
            recovery.backup(self.args)
        self.assertNotIn(self.key_bytes, self.output.read_bytes())
        with self.assertRaisesRegex(ValueError, "fresh issuer"):
            recovery.restore(self.args)
        self.state.rename(self.root / "retired")
        self.args.state = self.root / "replacement"
        publish_state = recovery.publish_state

        def restore_lost(*arguments):
            publish_state(*arguments)
            raise RuntimeError("lost restore reply")

        with (
            patch.object(recovery, "publish_state", side_effect=restore_lost),
            self.assertRaises(RuntimeError),
        ):
            recovery.restore(self.args)
        restored = self.args.state
        self.assertIsNone(acme.delivery.preflight(restored, acme.load(restored), acme.certificates))
        self.assertEqual(
            json.loads((restored / "restore.json").read_bytes())["backup_id"],
            inspected["backup_id"],
        )
        self.assertEqual(acme.account_keys(restored), acme.account_keys(self.root / "retired"))
        self.assertFalse((restored / "systemd").exists())

    @unittest.skipUnless(shutil.which("gpg"), "requires GnuPG")
    def test_authentication_failure_and_prepublication_failure_leave_no_destination(self):
        recovery.backup(self.args)
        self.state.rename(self.root / "retired")
        self.args.state = self.root / "replacement"
        wrong = self.root / "wrong-key"
        archive.generate_key(wrong)
        self.args.key = wrong
        with self.assertRaisesRegex(ValueError, "authentication"):
            recovery.restore(self.args)
        self.assertFalse(self.args.state.exists())
        self.args.key = self.key
        encrypted = bytearray(self.output.read_bytes())
        encrypted[-20] ^= 1
        corrupted = self.root / "corrupted.gpg"
        archive.write(corrupted, encrypted)
        self.args.archive = corrupted
        with self.assertRaisesRegex(ValueError, "authentication"):
            recovery.restore(self.args)
        self.assertFalse(self.args.state.exists())
        self.args.archive = self.output
        with (
            patch.object(recovery, "publish_state", side_effect=RuntimeError("power loss")),
            self.assertRaises(RuntimeError),
        ):
            recovery.restore(self.args)
        self.assertFalse(self.args.state.exists())
        recovery.restore(self.args)
        self.assertTrue(self.args.state.exists())

    def test_atomic_restore_does_not_replace_an_existing_empty_directory(self):
        prepared, destination = self.root / "prepared", self.root / "exists"
        prepared.mkdir(mode=0o700)
        destination.mkdir(mode=0o700)
        archive.write(prepared / "proof", "prepared")
        with self.assertRaises(FileExistsError):
            recovery.publish_state(prepared, destination)
        self.assertEqual(list(destination.iterdir()), [])
        self.assertTrue((prepared / "proof").exists())

    def test_backup_respects_the_live_issuer_lock(self):
        with acme.operation_lock(self.state):
            with self.assertRaises(BlockingIOError):
                recovery.backup(self.args)
        self.assertFalse(self.output.exists())

    def test_running_restartable_and_foreign_storage_users_are_refused(self):
        record = {
            "Id": "owned",
            "Config": {"Labels": {acme.LABEL: acme.identity(self.state)}},
            "State": {"Status": "exited", "Running": False},
            "HostConfig": {"RestartPolicy": {"Name": "no"}},
        }
        with patch.object(acme, "inspect", return_value=record):
            self.assertEqual(stopped_issuer(self.state, 0), record)
            record["State"]["Running"] = True
            with self.assertRaisesRegex(ValueError, "must be stopped"):
                stopped_issuer(self.state, 0)
            record["State"]["Running"] = False
            record["HostConfig"]["RestartPolicy"]["Name"] = "always"
            with self.assertRaisesRegex(ValueError, "restart policy"):
                stopped_issuer(self.state, 0)
        foreign = {
            "Id": "foreign",
            "Mounts": [{"Type": "bind", "Source": str(self.state / "client")}],
        }
        with (
            patch.object(acme, "run_command", return_value=b"foreign\n"),
            patch.object(acme, "inspect", return_value=foreign),
        ):
            with self.assertRaisesRegex(ValueError, "another container mounts"):
                check_storage_users([self.state], 0)


if __name__ == "__main__":
    unittest.main()
