"""Identify and verify the locally built, locked certificate issuer."""

import hashlib
import json
import re

INPUTS = (
    "deploy/acme/issuer.Dockerfile",
    "deploy/acme/issuer.go.mod",
    "deploy/acme/issuer.go.sum",
    "LICENSE",
)


def snapshot(root):
    return (
        json.loads((root / "deploy/acme/images.json").read_bytes()),
        {name: (root / name).read_bytes() for name in INPUTS},
    )


def build_specification(pins, contents):
    if re.fullmatch(r"goacme/lego:[^@]+@sha256:[a-f0-9]{64}", pins["issuer"]) is None:
        raise ValueError("the issuer base must have a pinned digest")
    digest = hashlib.sha256(b"issuer-base\0" + pins["issuer"].encode() + b"\0")
    for name in INPUTS:
        digest.update(name.encode() + b"\0" + contents[name] + b"\0")
    fingerprint = digest.hexdigest()
    return {**pins, "issuer": "chipload-acme:" + fingerprint}, fingerprint


def specification(root):
    return build_specification(*snapshot(root))


def resolve_image(reference, fingerprint, run_command, lock_fd):
    records = json.loads(run_command(["docker", "image", "inspect", reference], lock_fd))
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise ValueError("invalid issuer image inspection")
    record = records[0]
    config = record.get("Config")
    if not isinstance(config, dict) or not isinstance(config.get("Labels"), dict):
        raise ValueError("invalid issuer image labels")
    labels = config["Labels"]
    image_id = record.get("Id", "")
    if (
        not isinstance(image_id, str)
        or re.fullmatch(r"sha256:[a-f0-9]{64}", image_id) is None
        or labels.get("org.chipload.acme-role") != "issuer"
        or labels.get("org.chipload.acme-sha256") != fingerprint
    ):
        raise ValueError("rebuild the certificate issuer with ./scripts/acme-images")
    return image_id
