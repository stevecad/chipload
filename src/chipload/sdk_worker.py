"""Trusted SDK service in a separate container with only repository storage."""

import os
import resource
import threading
import time


def main():
    memory = 256 * 1024**2
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CPU, (3, 60))
    lifetime = time.monotonic() + 60
    call_deadline = lifetime
    guard = threading.Lock()

    def activity(running):
        nonlocal call_deadline
        with guard:
            call_deadline = min(lifetime, time.monotonic() + 5) if running else lifetime

    def watchdog():
        while True:
            with guard:
                remaining = min(lifetime, call_deadline) - time.monotonic()
            if remaining <= 0:
                os._exit(124)
            time.sleep(min(remaining, 0.05))

    threading.Thread(target=watchdog, daemon=True).start()
    from .sdk_service import data_directory
    from .service_worker import main as serve

    def configuration(config):
        import sqlite3
        from contextlib import closing
        from urllib.parse import quote

        data_directory(config["database"])
        if not isinstance(config.get("admission_identity"), str):
            raise ValueError("SDK service requires a repository identity")
        # Refuse a wrong mount before Repository can write or migrate anything.
        uri = "file:" + quote(config["database"], safe="/") + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=0.1)) as db:
            row = db.execute("SELECT identity,state FROM admission_store WHERE id=1").fetchone()
        if row != (config["admission_identity"], "ready"):
            raise ValueError("SDK service repository identity mismatch")

    serve(activity=activity, configuration=configuration)


if __name__ == "__main__":
    main()
