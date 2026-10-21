"""Actual file locks survive metadata checks and concurrent repository opens."""

import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from chipload.lifecycle import Repository
from chipload.storage import private_database

LOCK_PROBE = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print('locked')
else:
    print('unlocked')
finally:
    os.close(fd)
"""


class StorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "repository.db"

    def assert_locked(self, path, expected):
        result = subprocess.run(
            [sys.executable, "-c", LOCK_PROBE, str(path)],
            check=True, capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.stdout.strip(), "locked" if expected else "unlocked")

    def test_connections_and_repository_startup_preserve_existing_sqlite_locks(self):
        repository = Repository(self.path)
        for store in (repository, repository.admission):
            with self.subTest(path=store.path):
                with store.transaction():
                    self.assert_locked(store.path, True)
                    with store.connection() as reader:
                        reader.execute("SELECT name FROM sqlite_schema").fetchall()
                    self.assert_locked(store.path, True)
                    Repository(self.path)
                    self.assert_locked(store.path, True)
                self.assert_locked(store.path, False)

    def test_concurrent_thread_startups_share_one_paired_database(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            repositories = list(pool.map(Repository, [self.path] * 4))
        self.assertEqual(len({repo.admission.identity for repo in repositories}), 1)
        for store in (repositories[0], repositories[0].admission):
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            with store.connection() as db:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_missing_repository_is_not_recreated_by_an_existing_handle(self):
        repository = Repository(self.path)
        self.path.unlink()
        with self.assertRaises(FileNotFoundError), repository.connection():
            self.fail("missing database was reopened")
        self.assertFalse(self.path.exists())

    def test_private_regular_files_required_without_following_symlinks(self):
        target = self.path.with_suffix(".target")
        target.touch(mode=0o600)
        self.path.symlink_to(target)
        for create in (False, True):
            with self.assertRaises(OSError):
                private_database(self.path, create=create)
        self.path.unlink()
        os.mkfifo(self.path, mode=0o600)
        with self.assertRaises(ValueError):
            private_database(self.path, create=True)
        self.path.unlink()
        self.path.touch(mode=0o644)
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            Repository(self.path)


if __name__ == "__main__":
    unittest.main()
