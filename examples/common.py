"""Build identity for trusted local examples, separate from the platform engine."""

import hashlib
import platform
from collections.abc import Callable, Iterable
from pathlib import Path

from chipload import Artifact, BuildIdentity, Shop, compile_shop


def build_example(factory: Callable[[], Shop], source_files: Iterable[Path]) -> Artifact:
    root = Path(__file__).resolve().parents[1]

    def files_digest(paths: Iterable[Path]) -> str:
        return hashlib.sha256(
            b"".join(
                path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes() + b"\0"
                for path in sorted(paths)
            )
        ).hexdigest()

    identity = BuildIdentity(
        source_sha256=files_digest({Path(__file__).resolve(), *source_files}),
        dependencies_sha256=hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest(),
        sdk_sha256=files_digest((root / "src/chipload").glob("*.py")),
        runtime=f"{platform.python_implementation()}-{platform.python_version()}",
    )
    return compile_shop(factory(), identity)
