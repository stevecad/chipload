"""Separate local build-queue process; the HTTP service never invokes Docker."""

import argparse
import logging
import signal
import threading
from pathlib import Path

from .lifecycle import BuildProcessor, Conflict, Repository
from .sandbox import DockerBuilder, WorkerEnvironment


def process_next(processor: BuildProcessor) -> bool:
    processor.recover_expired()
    with processor.repository.connection() as db:
        row = db.execute(
            "SELECT id FROM builds WHERE state='queued' ORDER BY created_at,id LIMIT 1"
        ).fetchone()
    if row is None:
        return False
    try:
        processor.run(row["id"])
    except Conflict:
        # A second broker may have claimed the selected job.
        pass
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path(".local/chipload.db"))
    parser.add_argument("--image", help="immutable image ID; defaults to .local/worker-image")
    parser.add_argument(
        "--once", action="store_true", help="recover and process at most one queued job"
    )
    args = parser.parse_args()
    image = args.image or Path(".local/worker-image").read_text().strip()
    processor = BuildProcessor(
        Repository(args.database), DockerBuilder(WorkerEnvironment.inspect(image))
    )
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    while not stopping.is_set():
        try:
            worked = process_next(processor)
        except Exception:
            logging.exception("Build queue processing failed")
            if args.once:
                raise
            stopping.wait(5)
            continue
        if args.once:
            return
        if not worked:
            stopping.wait(1)


if __name__ == "__main__":
    main()
