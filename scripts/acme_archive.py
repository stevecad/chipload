"""Bounded private issuer snapshots inside authenticated OpenPGP envelopes."""

import hashlib
import json
import os
import re
import ssl
import stat
import struct
import time
import uuid
from pathlib import Path

import recovery_archive as archive

from chipload.project import unique_json

MAGIC = b"CHIPLOAD-ACME-1\n"
MAX_FILES = 2048
MAX_FILE = 1024 * 1024
MAX_DATA = 64 * 1024 * 1024
MAX_HEADER = 1024 * 1024
MAX_ARCHIVE = MAX_DATA + MAX_HEADER + 1024 * 1024
REQUIRED = {"issuer.json", "client.json", "identity-ca.pem", "directory-ca.pem", "account.json"}
OPTIONAL = {"issued.json", "last-run.json", "binding.json", "restore.json"}


def safe_name(name):
    if not isinstance(name, str) or len(name) > 512:
        return False
    parts = name.split("/")
    if (
        len(parts) > 12
        or any(part in {"", ".", ".."} for part in parts)
        or "\\" in name
        or any(ord(c) < 32 or ord(c) > 126 for c in name)
    ):
        return False
    return True


def allowed(name):
    return safe_name(name) and (
        name in REQUIRED | OPTIONAL
        or name.startswith("client/")
        or re.fullmatch(
            r"bundles/[a-f0-9]{64}/(?:identity-ca\.pem|certificates/(?:api|identity)\.(?:crt|key))",
            name,
        )
        is not None
    )


def snapshot(state, acme):
    files, total = {}, 0
    entries = 0
    current = None
    for marker in (acme.MARKER, acme.delivery.PENDING):
        if acme.certificates.present(state / marker):
            raise ValueError("finish pending issuance and certificate delivery before backup")
    link = state / "current"
    if acme.certificates.present(link):
        if not link.is_symlink() or link.lstat().st_uid != os.getuid():
            raise ValueError("issuer current must be its owned relative bundle link")
        target = os.readlink(link)
        if re.fullmatch(r"bundles/[a-f0-9]{64}", target) is None:
            raise ValueError("issuer current points outside its immutable bundles")
        current = target.split("/")[1]

    def visit(directory):
        nonlocal total, entries
        for path in sorted(directory.iterdir()):
            name = path.relative_to(state).as_posix()
            entries += 1
            if entries > MAX_FILES * 2 or not safe_name(name):
                raise ValueError("issuer state exceeds its path or entry limit")
            if name == "current":
                continue
            if name == ".operation.lock":
                archive.private(path)
                continue
            # Generated units are tied to the old checkout and absolute state path.
            if name == "systemd":
                archive.private(path, directory=True)
                continue
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                archive.private(path, directory=True)
                if name.split("/", 1)[0] not in {"client", "bundles"}:
                    raise ValueError("unexpected directory in issuer state")
                visit(path)
                continue
            archive.private(path)
            if not allowed(name) or info.st_nlink != 1:
                raise ValueError("unexpected issuer path or hard link")
            if not 0 <= info.st_size <= MAX_FILE:
                raise ValueError("issuer file exceeds its backup limit")
            files[name] = {"size": info.st_size, "sha256": archive.digest(path)}
            total += info.st_size
            if len(files) > MAX_FILES or total > MAX_DATA:
                raise ValueError("issuer backup exceeds its file or byte limit")

    visit(state)
    if not REQUIRED <= files.keys():
        raise ValueError("issuer backup requires an established account and complete configuration")
    return files, current


def validate(state, acme, current):
    values = acme.load(state)
    acme.check_account(state)
    if len(acme.account_keys(state)) != 1:
        raise ValueError("issuer backup requires one retained account key")
    key = next((state / "client/accounts").rglob("*.key"))
    acme.material.serialization.load_pem_private_key(acme.material.read(key), password=None)
    registration = unique_json(acme.material.read(key.parent / "account.json"))
    if not isinstance(registration, dict):
        raise ValueError("invalid retained account registration")
    acme.material.certificates(state / "identity-ca.pem")
    ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cafile=state / "directory-ca.pem")
    if (state / "binding.json").exists():
        acme.delivery.read(state / "binding.json")
    if (state / "issued.json").exists() != (current is not None):
        raise ValueError("issued certificate receipt and current bundle disagree")
    if current is not None:
        bundle = state / "bundles" / current
        acme.certificates.private_stage(bundle)
        if (bundle / "identity-ca.pem").read_bytes() != (state / "identity-ca.pem").read_bytes():
            raise ValueError("current bundle and issuer CA trust differ")
        report = acme.material.inspect(bundle, values)
        issued = acme.delivery.read(state / "issued.json")
        if report["leaves"] != issued.get("leaves") or report["leaves"]["api"]["sha256"] != current:
            raise ValueError("issued certificate identity differs from its retained bundle")
        for role in ("api", "identity"):
            if (bundle / f"certificates/{role}.crt").read_bytes() != (
                state / "client/certificates/shop.crt"
            ).read_bytes() or (bundle / f"certificates/{role}.key").read_bytes() != (
                state / "client/certificates/shop.key"
            ).read_bytes():
                raise ValueError("client certificate state differs from the current bundle")
        # Old archives may contain expired leaves. Retain them for renewal, but
        # still reject a mismatched key; run validates new material before delivery.
        certificate = acme.material.certificates(bundle / "certificates/api.crt")[0]
        key = acme.material.serialization.load_pem_private_key(
            acme.material.read(bundle / "certificates/api.key"), password=None
        )
        encoding = acme.material.serialization.Encoding.DER
        form = acme.material.serialization.PublicFormat.SubjectPublicKeyInfo
        if certificate.public_key().public_bytes(encoding, form) != key.public_key().public_bytes(
            encoding, form
        ):
            raise ValueError("issued certificate and private key differ")
        from cryptography import x509

        try:
            names = certificate.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value
        except x509.ExtensionNotFound as error:
            raise ValueError("issued certificate is missing its configured hostnames") from error
        if not {values["api_host"], values["identity_host"]} <= set(
            names.get_values_for_type(x509.DNSName)
        ):
            raise ValueError("issued certificate does not cover the configured hostnames")
    return values


def pack(state, target, acme):
    files, current = snapshot(state, acme)
    validate(state, acme, current)
    manifest = {
        "format": 1,
        "backup_id": uuid.uuid4().hex,
        "created_at": int(time.time()),
        "image": acme.IMAGES["issuer"],
        "source": str(state),
        "source_uid": os.getuid(),
        "current": current,
        "files": files,
    }
    header = json.dumps(manifest, sort_keys=True).encode()
    if len(header) > MAX_HEADER:
        raise ValueError("issuer recovery header exceeds its limit")
    archive.write(target, b"")
    with target.open("wb") as output:
        output.write(MAGIC + struct.pack(">I", len(header)) + header)
        for name in sorted(files):
            payload = acme.material.read(state / name)
            if (
                len(payload) != files[name]["size"]
                or hashlib.sha256(payload).hexdigest() != files[name]["sha256"]
            ):
                raise ValueError("issuer files changed during backup")
            output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    if snapshot(state, acme) != (files, current):
        raise ValueError("issuer state changed during backup")
    return manifest


def header(incoming, acme):
    if incoming.read(len(MAGIC)) != MAGIC:
        raise ValueError("unsupported issuer recovery archive")
    raw = incoming.read(4)
    if len(raw) != 4:
        raise ValueError("truncated issuer recovery header")
    length = struct.unpack(">I", raw)[0]
    if not 1 <= length <= MAX_HEADER:
        raise ValueError("issuer recovery header exceeds its limit")
    payload = incoming.read(length)
    if len(payload) != length:
        raise ValueError("truncated issuer recovery header")
    manifest = unique_json(payload)
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("format")) is not int
        or manifest["format"] != 1
        or manifest.get("image") != acme.IMAGES["issuer"]
        or re.fullmatch(r"[a-f0-9]{32}", manifest.get("backup_id", "")) is None
        or type(manifest.get("source_uid")) is not int
        or not 0 < manifest["source_uid"] < 2**32 - 1
        or type(manifest.get("created_at")) is not int
    ):
        raise ValueError("invalid issuer recovery contract; use its pinned client version")
    source = Path(manifest["source"])
    if not source.is_absolute() or ".." in source.parts:
        raise ValueError("invalid original issuer path")
    current = manifest.get("current")
    if current is not None and re.fullmatch(r"[a-f0-9]{64}", current) is None:
        raise ValueError("invalid current bundle identity")
    files = manifest.get("files")
    if not isinstance(files, dict) or not REQUIRED <= files.keys() or len(files) > MAX_FILES:
        raise ValueError("invalid issuer recovery file inventory")
    total = 0
    for name, info in files.items():
        if (
            not allowed(name)
            or not isinstance(info, dict)
            or type(info.get("size")) is not int
            or not 0 <= info["size"] <= MAX_FILE
            or re.fullmatch(r"[a-f0-9]{64}", info.get("sha256", "")) is None
            or any(str(parent) in files for parent in Path(name).parents)
        ):
            raise ValueError("invalid issuer recovery file entry")
        total += info["size"]
    if total > MAX_DATA:
        raise ValueError("issuer recovery payload exceeds its limit")
    return manifest


def unpack(source, state, acme):
    archive.private(source)
    if source.stat().st_size > MAX_ARCHIVE:
        raise ValueError("issuer recovery archive exceeds its limit")
    with source.open("rb") as incoming:
        manifest = header(incoming, acme)
        state.mkdir(mode=0o700)
        (state / "client").mkdir(mode=0o700)
        (state / "bundles").mkdir(mode=0o700)
        directories = {
            parent
            for name in manifest["files"]
            for parent in Path(name).parents
            if parent != Path(".")
        }
        for relative in sorted(directories, key=lambda path: (len(path.parts), str(path))):
            (state / relative).mkdir(mode=0o700, exist_ok=True)
            archive.private(state / relative, directory=True)
        for name in sorted(manifest["files"]):
            entry = manifest["files"][name]
            payload = incoming.read(entry["size"])
            if (
                len(payload) != entry["size"]
                or hashlib.sha256(payload).hexdigest() != entry["sha256"]
            ):
                raise ValueError("issuer recovery file is truncated or changed")
            archive.write(state / name, payload)
        if incoming.read(1):
            raise ValueError("unexpected trailing issuer recovery data")
    current = manifest["current"]
    if current is not None:
        (state / "current").symlink_to("bundles/" + current)
    if snapshot(state, acme) != (manifest["files"], current):
        raise ValueError("restored inventory differs from the authenticated snapshot")
    validate(state, acme, current)
    archive.sync_tree(state)
    return manifest
