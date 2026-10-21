"""Render reviewable user-systemd renewal units; enabling them is an operator action."""

import sys
from pathlib import Path

import recovery_archive as archive


def quote(value):
    value = str(value)
    if any(ord(character) < 32 for character in value):
        raise ValueError("systemd paths cannot contain control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def write(state, directory, name, root, deployment):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    archive.private(directory, directory=True)
    service = directory / (name + ".service")
    timer = directory / (name + ".timer")
    if service.exists() or service.is_symlink() or timer.exists() or timer.is_symlink():
        raise ValueError("renewal unit files already exist; review them before replacing")
    executable = " ".join(
        quote(argument)
        for argument in (
            Path(sys.executable).absolute(),
            "-E",
            "-s",
            "-B",
            root / "scripts/tls-acme",
            "--state",
            state,
            "run",
        )
    )
    archive.write(
        service,
        f"""[Unit]
Description=Chipload ACME renewal and certificate installation

[Service]
Type=oneshot
ExecStart=:{executable}
TimeoutStartSec=20min
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={quote(state)} {quote(deployment)}
MemoryMax=512M
TasksMax=128
LimitCORE=0
""",
    )
    archive.write(
        timer,
        f"""[Unit]
Description=Check Chipload certificates twice daily

[Timer]
OnCalendar=*-*-* 00,12:00:00 UTC
RandomizedDelaySec=30min
Persistent=yes
Unit={name}.service

[Install]
WantedBy=timers.target
""",
    )
    archive.sync_directory(directory)
    return service, timer
