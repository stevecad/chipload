"""Bounded confirmation of removal for Chipload's disposable containers."""

import re
import subprocess
import time

CLEANUP_SECONDS = 20


class SandboxError(RuntimeError):
    pass


def _absent(result):
    return result.returncode != 0 and any(
        message in result.stderr.lower()
        for message in (b"no such container", b"no such object")
    )


def _remove(name, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    try:
        result = subprocess.run(
            ["docker", "rm", "--force", name],
            capture_output=True,
            timeout=min(5, remaining),
        )
        if result.returncode == 0 or _absent(result):
            return True
    except subprocess.TimeoutExpired:
        pass  # The daemon may have committed removal despite the lost response.
    except OSError:
        return False

    # A concurrent remover can own the operation. Observe its outcome rather
    # than treating "removal in progress" or a CLI timeout as proof of absence.
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            result = subprocess.run(
                ["docker", "container", "inspect", "--format", "{{.Id}}", name],
                capture_output=True,
                timeout=min(1, remaining),
            )
        except subprocess.TimeoutExpired:
            continue
        except OSError:
            return False
        if _absent(result):
            return True
        if result.returncode:
            return False  # Daemon/permission errors cannot establish absence.
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    return False


def remove_containers(*names, guard=None):
    if not 1 <= len(names) <= 2 or any(
        not re.fullmatch(r"chipload-(?:build|sdk|code)-[0-9a-f]{32}", name) for name in names
    ):
        raise ValueError("invalid worker container names")
    deadline = time.monotonic() + CLEANUP_SECONDS
    failed = []
    unresolved = None
    for index, name in enumerate(names):
        # Give every container a share of the remaining budget, including when
        # its sibling stays present. Recovery's 30-second claim covers this
        # complete attempt and the subsequent database commit.
        at = time.monotonic()
        share = max(0, deadline - at) / (len(names) - index)
        if guard is not None:
            try:
                guard.before_remove(name, at + share)
            except SandboxError as error:
                unresolved = error
                continue
        if not _remove(name, at + share):
            failed.append(name)
    if unresolved is not None:
        raise unresolved
    if failed:
        raise SandboxError("could not confirm removal of worker containers: " + ", ".join(failed))
