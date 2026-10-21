"""Private SQLite files without interfering with SQLite's descriptor management."""

import errno
import os
import stat
import threading
from pathlib import Path

_creation_lock = threading.Lock()


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
