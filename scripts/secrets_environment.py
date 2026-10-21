"""Identify the locked OpenBao build used by development and private deployments."""

import hashlib
import json
import re
import subprocess

INPUTS = (
    "deploy/secrets/openbao.Dockerfile",
    "deploy/secrets/openbao.go.mod",
    "deploy/secrets/openbao.go.sum",
    "LICENSE",
)


def snapshot(root):
    return {name: (root / name).read_bytes() for name in INPUTS}


def build_specification(contents):
    digest = hashlib.sha256(b"chipload-openbao\0")
    for name in INPUTS:
        digest.update(name.encode() + b"\0" + contents[name] + b"\0")
    fingerprint = digest.hexdigest()
    return "chipload-openbao:" + fingerprint, fingerprint


def specification(root):
    return build_specification(snapshot(root))


def resolve_image(reference, fingerprint):
    records = json.loads(
        subprocess.check_output(["docker", "image", "inspect", reference], timeout=15)
    )
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise ValueError("invalid OpenBao image inspection")
    record = records[0]
    config = record.get("Config")
    if not isinstance(config, dict) or not isinstance(config.get("Labels"), dict):
        raise ValueError("invalid OpenBao image labels")
    labels = config["Labels"]
    image_id = record.get("Id", "")
    if (
        not isinstance(image_id, str)
        or re.fullmatch(r"sha256:[a-f0-9]{64}", image_id) is None
        or labels.get("org.chipload.secrets-role") != "openbao"
        or labels.get("org.chipload.secrets-sha256") != fingerprint
    ):
        raise ValueError("rebuild OpenBao with ./scripts/build-secrets")
    return image_id
