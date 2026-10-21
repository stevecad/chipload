"""Restricted, reproducible dependency input for operator-built shop environments."""

import hashlib
import re

MAX_LOCK_BYTES = 2 * 1024 * 1024


def requirements(data):
    """Accept exact PyPI pins and SHA-256 hashes, never pip directives or URLs."""
    if len(data) > MAX_LOCK_BYTES:
        raise ValueError("dependency lock exceeds 2 MiB")
    try:
        content = data.decode("ascii")
    except UnicodeDecodeError:
        raise ValueError("dependency lock must contain ASCII requirements") from None
    entries, pending = {}, ""
    for line in content.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        continued = line.endswith("\\")
        pending += " " + (line[:-1] if continued else line)
        if continued:
            continue
        fields = pending.split()
        pending = ""
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][A-Za-z0-9.!+_-]*)", fields[0])
        if (
            not match
            or len(fields) < 2
            or any(not re.fullmatch(r"--hash=sha256:[0-9a-f]{64}", field) for field in fields[1:])
        ):
            raise ValueError(
                "use exact name==version pins with SHA-256 hashes; directives, URLs and markers are unsupported"
            )
        name = re.sub(r"[-_.]+", "-", match[1]).lower()
        if name == "chipload" or name in entries:
            raise ValueError("dependency names must be unique and cannot replace Chipload")
        entries[name] = f"{name}=={match[2]} " + " ".join(sorted(set(fields[1:])))
        if len(entries) > 1024:
            raise ValueError("dependency lock exceeds 1024 packages")
    if pending:
        raise ValueError("unfinished dependency continuation")
    if not entries:
        raise ValueError("dependency lock must contain at least one pinned package")
    return ("\n".join(entries[name] for name in sorted(entries)) + "\n").encode("ascii")


def fingerprint(core_lock, shop_lock=b"", native_recipe=b""):
    core = hashlib.sha256(core_lock).hexdigest()
    if not shop_lock and not native_recipe:
        return core
    return hashlib.sha256(
        b"chipload-shop-environment-1\0" + core.encode() + b"\0" + shop_lock + b"\0" + native_recipe
    ).hexdigest()
