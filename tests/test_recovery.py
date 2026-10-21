"""Recovery preserves durable state, authenticates archives, and fails closed."""

import contextlib
import io
import shutil
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from recovery_support import archive, script

from chipload.access import ACTIONS, Grant, Principal
from chipload.lifecycle import Repository, ShopService
from chipload.quotas import QuotaPolicy
from chipload.request_limits import RateLimited, RequestLimiter
from examples.export_project import project

AT = 1_792_454_400_000


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "original"
        self.repository = Repository(self.data / "chipload.db")
        self.service = ShopService(self.repository)
        self.owner = Principal(
            subject="owner", grants=(Grant(shop_id="thermal-example", actions=ACTIONS),)
        )
        self.saved = self.service.save_source(self.owner, project(), expected_head=None)

    def test_python_runs_must_settle_before_backup_and_journals_survive(self):
        from chipload.program_model import RunPython
        from chipload.programs import ProgramService

        programs = ProgramService(self.repository)
        job = programs.submit(
            self.owner, RunPython(shop_id="thermal-example", request_key="backup", code="result=1")
        )
        with self.assertRaisesRegex(ValueError, "unfinished Python"):
            archive.validate_pair(self.data)
        programs.cancel(self.owner, job.shop_id, job.id)
        destination = self.root / "snapshot"
        archive.snapshot_pair(self.data, destination)
        restored = ProgramService(Repository(destination / "chipload.db"))
        self.assertEqual(restored.read(self.owner, job.shop_id, job.id).state, "cancelled")

    def contents(self):
        directory = self.root / "contents"
        directory.mkdir(mode=0o700)
        for name in archive.STATE_FILES:
            archive.write(directory / name, b"fixture")
        archive.write(directory / "keycloak.dump", b"PGDMP fixture")
        archive.snapshot_pair(self.data, directory / "data")
        return directory

    def test_snapshot_includes_wal_and_preserves_policy_spent_credit_and_source(self):
        with (
            self.repository.connection() as main,
            self.repository.admission.connection() as capacity,
        ):
            main.execute("PRAGMA wal_autocheckpoint=0")
            capacity.execute("PRAGMA wal_autocheckpoint=0")
            policy = self.repository.read_quotas()["policy"]
            policy["requests"]["identity"] = {"per_minute": 1, "burst": 1}
            self.repository.set_quotas(QuotaPolicy.model_validate(policy), expected_generation=0)
            RequestLimiter(self.repository, clock=lambda: AT).identity("spent")
            self.assertGreater(Path(str(self.repository.admission.path) + "-wal").stat().st_size, 0)
            destination = self.root / "snapshot"
            archive.snapshot_pair(self.data, destination)
        restored = Repository(destination / "chipload.db")
        self.assertEqual(restored.read_quotas()["generation"], 1)
        with self.assertRaises(RateLimited):
            RequestLimiter(restored, clock=lambda: AT).identity("spent")
        self.assertEqual(
            ShopService(restored).get_source(self.owner, "thermal-example"), self.saved
        )
        self.assertEqual(
            {p.name for p in destination.iterdir()}, {"chipload.db", "chipload.db.admission"}
        )

    def test_snapshot_holds_both_writer_locks_and_never_creates_missing_storage(self):
        validate = archive.validate_pair

        def inspect(directory):
            if directory == self.data:
                for name in ("chipload.db", "chipload.db.admission"):
                    with contextlib.closing(sqlite3.connect(directory / name, timeout=0)) as db:
                        with self.assertRaises(sqlite3.OperationalError):
                            db.execute("BEGIN IMMEDIATE")
            validate(directory)

        with patch.object(archive, "validate_pair", side_effect=inspect):
            archive.snapshot_pair(self.data, self.root / "copy")
        self.repository.admission.path.unlink()
        with self.assertRaises(FileNotFoundError):
            archive.snapshot_pair(self.data, self.root / "missing")
        self.assertFalse(self.repository.admission.path.exists())

    def test_mismatched_or_running_repositories_are_not_backed_up(self):
        other = Repository(self.root / "other/chipload.db")
        original = self.repository.admission.path.read_bytes()
        self.repository.admission.path.write_bytes(other.admission.path.read_bytes())
        with self.assertRaisesRegex(ValueError, "matched pair"):
            archive.snapshot_pair(self.data, self.root / "wrong")
        self.repository.admission.path.write_bytes(original)
        self.service.request_build(self.owner, "thermal-example", self.saved.revision)
        self.repository.claim_next(30)
        with self.assertRaisesRegex(ValueError, "running builds"):
            archive.snapshot_pair(self.data, self.root / "running")

    def test_archive_round_trip_uses_fixed_private_paths_and_rejects_wrong_versions(self):
        contents = self.contents()
        target = self.root / "plain.tar"
        metadata = {"backup_id": "a" * 32, "created_at": "2026-10-20T00:00:00+00:00"}
        manifest = archive.pack(contents, target, metadata)
        restored = self.root / "restored"
        self.assertEqual(archive.unpack(target, restored), manifest)
        for name in manifest["files"]:
            self.assertEqual((restored / name).read_bytes(), (contents / name).read_bytes())
            self.assertEqual((restored / name).stat().st_mode & 0o777, 0o600)
        with patch.object(archive, "contract", return_value={"sdk": "different"}):
            with self.assertRaisesRegex(ValueError, "version differs"):
                archive.unpack(target, self.root / "version")
        with self.assertRaisesRegex(ValueError, "byte limit"):
            archive.unpack(target, self.root / "limit", max_bytes=1)

    def test_launch_receipts_survive_backup_and_unresolved_intent_blocks_it(self):
        receipt = ("chipload-build-" + "a" * 32, "builds", "a" * 32, "b" * 32, 1)
        with self.repository.transaction() as db:
            db.execute("INSERT INTO worker_launches VALUES(?,?,?,?,?)", receipt)
        destination = self.root / "snapshot"
        archive.snapshot_pair(self.data, destination)
        with Repository(destination / "chipload.db").connection() as db:
            self.assertEqual(tuple(db.execute("SELECT * FROM worker_launches").fetchone()), receipt)
        # Even inconsistent/orphaned intent cannot disappear into a backup.
        with self.repository.transaction() as db:
            db.execute("UPDATE worker_launches SET settled=0")
        with self.assertRaisesRegex(ValueError, "unresolved container creation"):
            archive.snapshot_pair(self.data, self.root / "unresolved")

    def test_archive_rejects_traversal_links_duplicates_missing_data_and_changed_bytes(self):
        target = self.root / "plain.tar"
        archive.pack(self.contents(), target, {"backup_id": "a" * 32})
        with tarfile.open(target) as tar:
            entries = [(member, tar.extractfile(member).read()) for member in tar]
        for index, kind in enumerate(
            ("traversal", "absolute", "symlink", "duplicate", "manifest", "missing", "bytes")
        ):
            bad = self.root / f"bad-{index}.tar"
            with tarfile.open(bad, "w") as tar:
                for member, body in entries:
                    if kind == "missing" and member.name == "data/chipload.db.admission":
                        continue
                    if kind == "bytes" and member.name == "realm.json":
                        body = b"changed"
                    tar.addfile(member, io.BytesIO(body))
                    if kind == "manifest" and member.name == "manifest.json":
                        tar.addfile(member, io.BytesIO(body))
                if kind in {"traversal", "absolute", "symlink", "duplicate"}:
                    name = {
                        "traversal": "../escape",
                        "absolute": str(self.root / "escape"),
                        "symlink": "realm.json",
                        "duplicate": "realm.json",
                    }[kind]
                    member = tarfile.TarInfo(name)
                    if kind == "symlink":
                        member.type, member.linkname = tarfile.SYMTYPE, "/etc/passwd"
                    tar.addfile(member, io.BytesIO())
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                archive.unpack(bad, self.root / f"restored-{index}")
            self.assertFalse((self.root / "escape").exists())

    @unittest.skipUnless(shutil.which("gpg"), "requires GnuPG")
    def test_encryption_authenticates_before_restore_and_rejects_tampering_wrong_keys_and_limits(
        self,
    ):
        key, other = self.root / "key", self.root / "other-key"
        archive.generate_key(key)
        archive.generate_key(other)
        plain, encrypted = self.root / "plain", self.root / "encrypted"
        payload = b"private credential fixture" * 1024
        archive.write(plain, payload)
        archive.crypt(plain, encrypted, key)
        self.assertNotIn(b"private credential fixture", encrypted.read_bytes())
        archive.crypt(encrypted, self.root / "decoded", key, decrypt=True)
        self.assertEqual((self.root / "decoded").read_bytes(), payload)
        ciphertext = encrypted.read_bytes()
        for index, (content, used_key) in enumerate(
            (
                (ciphertext, other),
                (ciphertext[:-1], key),
                (ciphertext[:100] + bytes([ciphertext[100] ^ 1]) + ciphertext[101:], key),
            )
        ):
            damaged = self.root / f"damaged-{index}"
            archive.write(damaged, content)
            output = self.root / f"output-{index}"
            with self.assertRaisesRegex(ValueError, "authentication"):
                archive.crypt(damaged, output, used_key, decrypt=True)
            self.assertFalse(output.exists())
        with self.assertRaisesRegex(ValueError, "byte limit"):
            archive.crypt(encrypted, self.root / "bounded", key, decrypt=True, max_bytes=100)

    def test_private_keys_are_never_overwritten_or_followed_through_links(self):
        key = self.root / "key"
        archive.generate_key(key)
        original = key.read_bytes()
        with self.assertRaises(FileExistsError):
            archive.generate_key(key)
        self.assertTrue(key.read_bytes() == original)
        alias = self.root / "alias"
        alias.symlink_to(key)
        with self.assertRaises(ValueError):
            archive.check_key(alias)
        key.chmod(0o644)
        with self.assertRaises(ValueError):
            archive.check_key(key)

    def test_aliases_cannot_place_a_recovery_key_inside_deployment_state(self):
        state = self.root / "state"
        state.mkdir(mode=0o700)
        key = state / "key"
        archive.generate_key(key)
        alias = self.root / "alias"
        alias.symlink_to(state, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "outside deployment state"):
            script.backup(state, self.root / "backup.gpg", alias / "key", archive.MAX_BYTES)
        state.chmod(0o755)
        with self.assertRaises(ValueError):
            archive.check_key(key)

    def test_operation_lock_and_incomplete_markers_block_application_startup(self):
        with script.stack.operation_lock(self.root):
            with self.assertRaisesRegex(ValueError, "another deployment"):
                with script.stack.operation_lock(self.root):
                    self.fail("acquired a competing lock")
        for name in (script.BACKUP_MARKER, script.RESTORE_MARKER, ".credentials-incomplete.json"):
            marker = self.root / name
            archive.write(marker, "{}")
            with (
                patch.object(script.stack.runtime, "docker_socket"),
                patch.object(script.stack, "deployment") as deployment,
                patch("sys.argv", ["tls-stack", "--state", str(self.root), "up"]),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as error:
                    script.stack.main()
                self.assertEqual(error.exception.code, 1)
                deployment.assert_not_called()
            marker.unlink()

    def test_restore_never_overwrites_an_existing_state_directory(self):
        key, backup = self.root / "key", self.root / "backup"
        archive.generate_key(key)
        archive.write(backup, b"fixture")
        with self.assertRaisesRegex(ValueError, "new state directory"):
            script.restore(self.data, backup, key, "fixture", False, archive.MAX_BYTES)
        self.assertEqual(self.service.get_source(self.owner, "thermal-example"), self.saved)

    def test_restore_does_not_adopt_a_volume_created_by_a_competing_attempt(self):
        competing = [
            {
                "Name": "recovered_keycloak-data",
                "Labels": {"org.chipload.restore-id": "other-attempt"},
            }
        ]
        with (
            patch.object(script, "records", side_effect=[[], competing]),
            patch.object(script, "run") as create,
        ):
            with self.assertRaisesRegex(ValueError, "does not belong"):
                script.restore_volume("recovered", "this-attempt")
            create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
