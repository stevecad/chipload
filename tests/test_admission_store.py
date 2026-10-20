"""Capacity-store migration, interruption recovery, and repository pairing."""

import hashlib
import json
import multiprocessing
import os
import shutil
import sqlite3
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from chipload.admission import AdmissionStore
from chipload.lifecycle import SCHEMA, Repository
from chipload.quotas import QUOTA_SCHEMA, QuotaPolicy
from chipload.request_limits import REQUEST_SCHEMA, RateLimited, RequestCapacity, RequestLimiter

AT = 1_792_454_400_000


def repository_identity(path):
    return Repository(path).admission.identity


class AdmissionStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "platform.db"

    def legacy(self, version=4):
        policy = QuotaPolicy().model_dump(mode="json")
        policy["shop"]["pending_builds"] = 3
        policy["requests"]["identity"] = {"per_minute": 1, "burst": 1}
        if version == 3:
            del policy["requests"]
        with sqlite3.connect(self.path) as db:
            db.executescript(
                SCHEMA
                + "ALTER TABLE builds ADD COLUMN actor TEXT NOT NULL DEFAULT 'legacy';"
                + QUOTA_SCHEMA
                + (REQUEST_SCHEMA if version == 4 else "")
            )
            payload = json.dumps(policy).encode()
            db.execute("INSERT INTO quota_policy VALUES(1,7,?)", (payload,))
            for generation in (0, 7):
                db.execute(
                    "INSERT INTO quota_changes VALUES(?,?,?)",
                    (generation, payload, "2026-10-20T00:00:00+00:00"),
                )
            if version == 4:
                db.execute(
                    "INSERT INTO request_buckets VALUES(?,?,?,?,?,?,?)",
                    ("identity", hashlib.sha256(b"owner").digest(), 0, AT, 1, 1, AT + 60_000),
                )
            db.execute(f"PRAGMA user_version={version}")
        os.chmod(self.path, 0o600)

    def assert_migrated(self, repository):
        state = repository.read_quotas()
        self.assertEqual(state["generation"], 7)
        self.assertEqual(state["policy"]["shop"]["pending_builds"], 3)
        self.assertEqual(state["usage"]["deployment"]["request_buckets"], 1)
        limiter = RequestLimiter(repository, clock=lambda: AT)
        with self.assertRaises(RateLimited):
            limiter.identity("owner")
        with repository.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(db.execute("SELECT state FROM admission_store").fetchone()[0], "ready")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            self.assertTrue(tables.isdisjoint({"request_buckets", "quota_policy", "quota_changes"}))
        with repository.admission.connection() as db:
            self.assertEqual(
                [
                    r[0]
                    for r in db.execute("SELECT generation FROM quota_changes ORDER BY generation")
                ],
                [0, 7],
            )
        self.assertEqual(repository.admission.path.stat().st_mode & 0o777, 0o600)

    def test_schema_four_migration_preserves_policy_history_and_spent_allowance(self):
        self.legacy()
        self.assert_migrated(Repository(self.path))
        self.assert_migrated(Repository(self.path))

    def test_offline_restore_reenables_wal_without_resetting_policy_or_credit(self):
        self.legacy()
        original = Repository(self.path)
        directory = self.path.parent / "restored"
        directory.mkdir(mode=0o700)
        for source in (original.path, original.admission.path):
            target = directory / source.name
            target.touch(mode=0o600)
            with (
                closing(sqlite3.connect(source)) as reader,
                closing(sqlite3.connect(target)) as copy,
            ):
                reader.backup(copy)
                copy.execute("PRAGMA journal_mode=DELETE")
        restored = Repository(directory / self.path.name)
        self.assert_migrated(restored)
        for target in (restored.path, restored.admission.path):
            with closing(sqlite3.connect(target)) as db:
                self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_schema_three_gains_default_request_limits_without_changing_other_limits(self):
        self.legacy(version=3)
        repository = Repository(self.path)
        state = repository.read_quotas()
        self.assertEqual(state["generation"], 7)
        self.assertEqual(state["policy"]["shop"]["pending_builds"], 3)
        self.assertEqual(state["policy"]["requests"]["peer"]["burst"], 40)
        self.assertEqual(state["usage"]["deployment"]["request_buckets"], 0)
        RequestLimiter(repository).identity("owner")

    def test_interruption_before_copy_leaves_a_resumable_pending_migration(self):
        self.legacy()
        with patch.object(AdmissionStore, "initialize", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                Repository(self.path)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(
                db.execute("SELECT state FROM admission_store").fetchone()[0], "pending"
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM request_buckets").fetchone()[0], 1)
        self.assert_migrated(Repository(self.path))

    def test_interruption_after_destination_commit_repeats_copy_without_resetting_credit(self):
        self.legacy()
        initialize = AdmissionStore.initialize

        def interrupted(store, source):
            initialize(store, source)
            raise RuntimeError("interrupted after copy")

        with patch.object(AdmissionStore, "initialize", interrupted):
            with self.assertRaisesRegex(RuntimeError, "interrupted after copy"):
                Repository(self.path)
        self.assertTrue(self.path.with_name(self.path.name + ".admission").exists())
        with sqlite3.connect(self.path) as db:
            self.assertEqual(
                db.execute("SELECT state FROM admission_store").fetchone()[0], "pending"
            )
        self.assert_migrated(Repository(self.path))

    def test_competing_startups_share_one_migration_and_store(self):
        self.legacy()
        with ProcessPoolExecutor(
            max_workers=4, mp_context=multiprocessing.get_context("spawn")
        ) as pool:
            identities = list(pool.map(repository_identity, [self.path] * 4))
        self.assertEqual(len(set(identities)), 1)
        repository = Repository(self.path)
        self.assertEqual(repository.admission.identity, identities[0])
        self.assert_migrated(repository)

    def test_missing_ready_store_is_not_recreated_with_fresh_allowances(self):
        repository = Repository(self.path)
        repository.admission.path.unlink()
        with self.assertRaises(FileNotFoundError):
            Repository(self.path)
        with self.assertRaises(RequestCapacity):
            RequestLimiter(repository).identity("owner")
        self.assertFalse(repository.admission.path.exists())

    def test_swapping_another_repositories_store_fails_closed(self):
        repository = Repository(self.path)
        other = Repository(self.path.parent / "other.db")
        shutil.copyfile(other.admission.path, repository.admission.path)
        with self.assertRaisesRegex(sqlite3.DatabaseError, "does not match"):
            Repository(self.path)
        with self.assertRaises(RequestCapacity):
            RequestLimiter(repository).identity("owner")

    def test_symlink_and_permissive_stores_are_rejected(self):
        repository = Repository(self.path)
        store = repository.admission.path
        os.chmod(store, 0o644)
        with self.assertRaises(sqlite3.DatabaseError):
            Repository(self.path)
        with self.assertRaises(RequestCapacity):
            RequestLimiter(repository).identity("owner")
        os.chmod(store, 0o600)
        backup = store.with_suffix(".backup")
        store.rename(backup)
        store.symlink_to(backup)
        with self.assertRaises(OSError):
            Repository(self.path)
        with self.assertRaises(RequestCapacity):
            RequestLimiter(repository).identity("owner")

    def test_existing_unrelated_database_is_not_overwritten_during_migration(self):
        self.legacy()
        destination = self.path.with_name(self.path.name + ".admission")
        with sqlite3.connect(destination) as db:
            db.execute("CREATE TABLE unrelated (value TEXT)")
            db.execute("INSERT INTO unrelated VALUES('preserve this')")
        os.chmod(destination, 0o600)
        with self.assertRaises(sqlite3.DatabaseError):
            Repository(self.path)
        with sqlite3.connect(destination) as db:
            self.assertEqual(
                db.execute("SELECT value FROM unrelated").fetchone()[0], "preserve this"
            )


if __name__ == "__main__":
    unittest.main()
