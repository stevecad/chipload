"""Offline secrets recovery preserves custody and refuses partial or hostile archives."""

import base64
import contextlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from secrets_support import archive, fixture_args, script

recovery = script.load("secrets_recovery_test", "scripts/secrets-recovery")


@unittest.skipUnless(shutil.which("gpg"), "requires GnuPG")
class SecretsRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chipload-secrets-recovery-unit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.gpg, args = fixture_args(self.root)
        self.addCleanup(
            lambda: subprocess.run(
                ["gpgconf", "--homedir", str(self.root / "gnupg"), "--kill", "gpg-agent"],
                capture_output=True,
            )
        )
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        for module in (script, recovery.stack):
            self.patches.enter_context(patch.object(module.executor.runtime, "docker_socket"))
            self.patches.enter_context(patch.object(module.executor, "private_network"))
            self.patches.enter_context(patch.object(module, "ownership", return_value=None))
        self.patches.enter_context(patch.object(recovery, "storage_users"))
        self.compose = self.patches.enter_context(patch.object(recovery.stack, "compose"))
        script.configure(args)
        self.state = args.state
        self.value = script.read_json(self.state / "deployment.json")
        (self.state / "data/raft/snapshots").mkdir(mode=0o700, parents=True)
        (self.state / "data/raft").chmod(0o700)
        for name in ("data/raft/raft.db", "data/vault.db", "audit/audit.jsonl"):
            archive.write(self.state / name, "offline fixture " + name)
        packet = base64.b64encode(b"\x85" + b"x" * 150).decode()
        archive.replace_json(
            self.state / "bootstrap.json",
            {
                "version": 1,
                "threshold": self.value["threshold"],
                "fingerprints": self.value["fingerprints"],
                "root_fingerprint": self.value["root_fingerprint"],
                "root_token": packet,
                "keys_base64": [packet] * 3,
            },
        )
        archive.replace_json(
            self.state / "initialized.json",
            {
                "version": 1,
                "owner": script.ownership_key(self.state, self.value),
                "cluster_id": "original-cluster",
            },
        )
        self.key, self.output = self.root / "key", self.root / "backup.gpg"
        archive.generate_key(self.key)
        self.args = SimpleNamespace(state=self.state, archive=self.output, key=self.key)

    def test_encrypted_roundtrip_fresh_install_custody_and_second_generation(self):
        recovery.backup(self.args)
        self.assertFalse((self.state / recovery.MARKER).exists())
        restored = self.root / "restored"
        self.args.state = restored
        result = recovery.restore(self.args)
        self.assertEqual(result["cluster_id"], "original-cluster")
        self.assertTrue(result["unseal_required"])
        self.assertEqual(
            (self.state / "bootstrap.json").read_bytes(), (restored / "bootstrap.json").read_bytes()
        )
        for path in restored.rglob("*"):
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        receipt = script.read_json(restored / "initialized.json")
        self.assertEqual(receipt["owner"], script.ownership_key(restored, self.value))
        with self.assertRaisesRegex(ValueError, "fresh"):
            recovery.restore(self.args)
        self.args.archive = self.root / "second.gpg"
        recovery.backup(self.args)
        self.args.state = self.root / "again"
        recovery.restore(self.args)
        self.assertEqual(
            script.read_json(self.args.state / "initialized.json")["cluster_id"], "original-cluster"
        )

    def test_interrupted_backup_blocks_startup_and_resumes_exact_intent(self):
        with patch.object(recovery, "pack", side_effect=OSError("disk failed")):
            with self.assertRaises(OSError):
                recovery.backup(self.args)
        self.assertTrue((self.state / recovery.MARKER).exists())
        with patch.object(script, "compose") as compose:
            for action in ("up", "reload", "unseal", "initialize", "status"):
                with self.assertRaisesRegex(ValueError, "incomplete backup"):
                    script.operate(SimpleNamespace(state=self.state, action=action))
            compose.assert_not_called()
        self.args.archive = self.root / "different.gpg"
        with self.assertRaisesRegex(ValueError, "original"):
            recovery.backup(self.args)
        self.args.archive = self.output
        recovery.backup(self.args)
        self.assertFalse((self.state / recovery.MARKER).exists())

    def test_lost_backup_completion_authenticates_existing_output_without_replacing_it(self):
        with patch.object(recovery, "finish_backup", side_effect=OSError("lost completion")):
            with self.assertRaises(OSError):
                recovery.backup(self.args)
        digest = archive.digest(self.output)
        with patch.object(recovery, "pack") as pack:
            recovery.backup(self.args)
            pack.assert_not_called()
        self.assertEqual(archive.digest(self.output), digest)

    def test_wrong_key_tampering_and_failed_install_leave_no_destination(self):
        recovery.backup(self.args)
        self.args.state = self.root / "restored"
        other = self.root / "other-key"
        archive.generate_key(other)
        self.args.key = other
        with self.assertRaisesRegex(ValueError, "authentication"):
            recovery.restore(self.args)
        self.assertFalse(self.args.state.exists())
        self.args.key = self.key
        with patch.object(recovery.os, "rename", side_effect=OSError("lost install")):
            with self.assertRaises(OSError):
                recovery.restore(self.args)
        self.assertFalse(self.args.state.exists())
        content = bytearray(self.output.read_bytes())
        content[-15] ^= 1
        self.output.write_bytes(content)
        with self.assertRaisesRegex(ValueError, "authentication"):
            recovery.restore(self.args)
        self.assertFalse(self.args.state.exists())

    def test_links_unknown_files_and_shared_modes_refuse_capture(self):
        path = self.state / "data/extra"
        for kind in ("symlink", "hardlink", "permissions", "unknown"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    path.symlink_to(self.key)
                elif kind == "hardlink":
                    os.link(self.key, path)
                elif kind == "permissions":
                    archive.write(path, "fixture")
                    path.chmod(0o644)
                else:
                    path = self.state / "unseal-share"
                    archive.write(path, "must never enter backup")
                with self.assertRaises(ValueError):
                    recovery.pack(self.state, self.root / "plain.tar", {"owner": "fixture"})
                path.unlink()

    def test_archive_path_link_digest_and_version_attacks_never_install(self):
        source = self.root / "plain.tar"
        intent = {"owner": script.ownership_key(self.state, self.value), "id": "fixture"}
        recovery.pack(self.state, source, intent)
        with tarfile.open(source, "r:") as tar:
            records = [
                (member, tar.extractfile(member).read() if member.isfile() else None)
                for member in tar
            ]
        for attack in (
            "traversal",
            "link",
            "duplicate",
            "digest",
            "missing",
            "version",
            "entry_limit",
        ):
            with self.subTest(attack=attack):
                output = self.root / f"{attack}.tar"
                with tarfile.open(output, "w") as tar:
                    for index, (original, payload) in enumerate(records):
                        header = tarfile.TarInfo(original.name)
                        header.type, header.size = original.type, original.size
                        if index == 0:
                            manifest = json.loads(payload)
                            if attack == "traversal":
                                manifest["entries"]["../escaped"] = {
                                    "directory": False,
                                    "size": 0,
                                    "sha256": "0" * 64,
                                }
                            if attack == "version":
                                manifest["contract"]["image"] = "wrong-image"
                            if attack == "entry_limit":
                                manifest["entries"]["data/vault.db"]["size"] = archive.MAX_BYTES + 1
                            payload = json.dumps(manifest).encode()
                            header.size = len(payload)
                        if header.name == "data/vault.db":
                            if attack == "missing":
                                continue
                            if attack == "link":
                                header.type, header.linkname, header.size, payload = (
                                    tarfile.SYMTYPE,
                                    "/outside",
                                    0,
                                    None,
                                )
                            if attack == "digest":
                                payload = b"x" * header.size
                        tar.addfile(header, io.BytesIO(payload) if payload is not None else None)
                        if attack == "duplicate" and header.name == "data/vault.db":
                            tar.addfile(header, io.BytesIO(payload))
                with self.assertRaises(ValueError):
                    recovery.unpack(output, self.root / attack)
        self.assertFalse((self.root / "escaped").exists())

    def test_active_storage_or_mount_mismatch_refuses_before_stopping(self):
        with patch.object(recovery, "storage_users", side_effect=ValueError("another container")):
            with self.assertRaisesRegex(ValueError, "another container"):
                recovery.backup(self.args)
        self.compose.assert_not_called()
        self.assertFalse((self.state / recovery.MARKER).exists())
        with self.assertRaisesRegex(ValueError, "storage differs"):
            recovery.owned_mounts(self.state, {"Mounts": []})

    def test_extended_headers_and_malformed_manifest_are_rejected_before_extraction(self):
        source = self.root / "hostile.tar"
        for kind in (tarfile.XHDTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_SPARSE):
            header = tarfile.TarInfo("extended")
            header.type, header.size = kind, 1024**3
            source.write_bytes(header.tobuf() + bytes(1024))
            with patch.object(recovery.tarfile, "open") as parser:
                with self.assertRaises(ValueError):
                    recovery.unpack(source, self.root / "destination")
                parser.assert_not_called()
            self.assertFalse((self.root / "destination").exists())
        with tarfile.open(source, "w", format=tarfile.USTAR_FORMAT) as output:
            header = tarfile.TarInfo("manifest.json")
            header.size = 2
            output.addfile(header, io.BytesIO(b"[]"))
        with self.assertRaisesRegex(ValueError, "deployment version"):
            recovery.unpack(source, self.root / "destination")

    def test_mount_scan_rejects_stopped_container_and_ancestor_mount(self):
        for source in (self.state, self.state.parent, self.state / "data"):
            container = {"Id": "foreign", "Mounts": [{"Type": "bind", "Source": str(source)}]}
            with patch.object(
                recovery.subprocess,
                "check_output",
                side_effect=["foreign\n", json.dumps([container])],
            ):
                # Use the real implementation underneath the setup mock.
                original = script.load("secrets_recovery_scan", "scripts/secrets-recovery")
                with self.assertRaisesRegex(ValueError, "another container"):
                    original.storage_users(self.state)

    def test_mount_scan_confirms_removed_container_and_refuses_live_inspection_failure(self):
        original = script.load("secrets_recovery_scan_race", "scripts/secrets-recovery")
        failure = subprocess.CalledProcessError(1, ["docker", "inspect", "foreign"])
        with patch.object(
            original.subprocess, "check_output", side_effect=["foreign\n", failure, ""]
        ):
            original.storage_users(self.state)
        with patch.object(
            original.subprocess, "check_output", side_effect=["foreign\n", failure, "foreign\n"]
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                original.storage_users(self.state)
