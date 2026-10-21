"""Private SQLite files without interfering with SQLite's descriptor management."""

import errno
import os
import sqlite3
import stat
import threading
import time
from pathlib import Path

_creation_lock = threading.Lock()


def enable_wal(db, *, timeout=5.0):
    """Bound contention during the idempotent journal-mode transition only."""
    if db.in_transaction:
        raise ValueError("WAL initialization requires no active transaction")
    deadline = time.monotonic() + timeout
    while True:
        try:
            mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode != "wal":
                raise sqlite3.DatabaseError("database requires WAL journaling")
            return
        except sqlite3.OperationalError as error:
            if getattr(error, "sqlite_errorcode", 0) & 255 != sqlite3.SQLITE_BUSY:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(.01, remaining))


def private_database(path: Path, *, create=False):
    # Closing any raw descriptor for an existing database can release SQLite's
    # POSIX locks in this process. Inspect metadata without opening the file.
    # Serialize creation/checks so no thread starts SQLite before the creator
    # closes its descriptor. O_EXCL also handles competing processes safely.
    with _creation_lock:
        if create:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise OSError(errno.ELOOP, "database path must not be a symlink", str(path))
        if not stat.S_ISREG(mode) or mode & 0o077:
            raise ValueError("database must be a regular owner-only file (0600)")
