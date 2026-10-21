"""Separate local build-queue process; the HTTP service never invokes Docker."""

import argparse
import logging
import signal
import threading
from pathlib import Path

from .executor import ExecutorSettings, private_json
from .lifecycle import BuildProcessor, Repository
from .sandbox import DockerBuilder, WorkerEnvironment


def process_next(processor: BuildProcessor) -> bool:
    processor.recover_expired()
    return processor.run_next()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path(".local/chipload.db"))
    parser.add_argument("--image", help="immutable image ID; defaults to .local/worker-image")
    parser.add_argument(
        "--settings", type=Path, help="operator-owned settings enabling scoped Python runs"
    )
    parser.add_argument(
        "--service-data",
        type=Path,
        help="repository directory on the Docker host, when it differs from --database's parent",
    )
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
    programs = None
    if args.settings:
        from .program_runner import ProgramProcessor
        from .program_sandbox import DockerProgramRunner

        def principal_for(subject):
            current = ExecutorSettings.model_validate(private_json(args.settings))
            if current.database.absolute() != processor.repository.path:
                raise ValueError("broker settings name another repository")
            return next((who for who in current.bindings if who.subject == subject), None)

        principal_for("")  # Validate the file and database before starting either loop.
        programs = ProgramProcessor(
            processor.repository,
            DockerProgramRunner(
                processor.builder.environment, processor.repository, host_data=args.service_data
            ),
            principal_for,
            stopping=stopping,
        )
    if programs and not args.once:

        def code_loop():
            while not stopping.is_set():
                try:
                    programs.recover_expired()
                    worked = programs.run_next()
                except Exception:
                    logging.error(
                        "Python queue processing failed; inspect private deployment state"
                    )
                    stopping.wait(5)
                    continue
                if not worked:
                    stopping.wait(0.5)

        thread = threading.Thread(target=code_loop, name="chipload-python-broker")
        thread.start()
    else:
        thread = None
    try:
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
                if programs and not worked:
                    programs.recover_expired()
                    programs.run_next()
                return
            if not worked:
                stopping.wait(1)
    finally:
        stopping.set()
        if thread:
            thread.join()


if __name__ == "__main__":
    main()
