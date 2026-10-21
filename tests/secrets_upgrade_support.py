"""Run the original pinned image/Compose contract with the unchanged archive writer.

Only fixture orchestration uses the old image-selection behavior. The server,
encrypted store, initialization, requests and archive bytes are real. Current
production startup never accepts this legacy image.
"""

import contextlib
import hashlib
import json
import subprocess
from unittest.mock import patch

from secrets_support import ROOT


@contextlib.contextmanager
def legacy_deployment(upgrades, *stacks):
    compose_file = ROOT / "tests/fixtures/secrets-compose-2.7.1.yaml"
    if hashlib.sha256(compose_file.read_bytes()).hexdigest() != upgrades.SOURCE["compose"]:
        raise ValueError("legacy fixture Compose differs from the reviewed source")
    reference = upgrades.SOURCE["image"]
    try:
        subprocess.check_output(["docker", "image", "inspect", reference], stderr=subprocess.PIPE, timeout=15)
    except subprocess.CalledProcessError:
        subprocess.run(["docker", "pull", reference], check=True, capture_output=True, timeout=180)

    def resolve_image(selected, fingerprint):
        if selected != reference:
            raise ValueError("unexpected legacy fixture image")
        record = json.loads(subprocess.check_output(["docker", "image", "inspect", reference], timeout=15))[0]
        digest = reference.split("@", 1)[1]
        if not any(item.endswith("@" + digest) for item in record.get("RepoDigests", [])):
            raise ValueError("legacy fixture image digest differs")
        return record["Id"]

    def ownership(value, owner):
        identifiers = subprocess.check_output([
            "docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + value["name"],
        ], text=True, timeout=15).split()
        if not identifiers:
            return None
        if len(identifiers) != 1:
            raise ValueError("legacy fixture has unexpected containers")
        record = json.loads(subprocess.check_output(["docker", "inspect", identifiers[0]], timeout=15))[0]
        if (record["Config"]["Image"] != reference
                or record["Config"]["Labels"].get("org.chipload.secrets-owner") != owner
                or record["Image"] != resolve_image(reference, None)):
            raise ValueError("legacy fixture container belongs to another deployment")
        return record

    def deployment(original, state):
        value, command, environment, container = original(state)
        command = [*command[:-1], str(compose_file)]
        return value, command, environment, container

    with contextlib.ExitStack() as changes:
        changes.enter_context(patch.object(stacks[0].image_environment, "resolve_image", new=resolve_image))
        for stack in stacks:
            changes.enter_context(patch.object(stack, "IMAGE", reference))
            changes.enter_context(patch.object(stack, "ownership", new=ownership))
            original = stack.deployment
            changes.enter_context(patch.object(
                stack, "deployment", new=lambda state, original=original: deployment(original, state),
            ))
        yield
