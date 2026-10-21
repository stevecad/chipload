"""Bind one issuer to an existing deployment and recover its own certificate delivery."""

import json
import re
import uuid
from pathlib import Path

import recovery_archive as archive

BINDING = "binding.json"
PENDING = ".delivery-incomplete.json"


def read(path):
    archive.private(path)
    if path.stat().st_size > 64 * 1024:
        raise ValueError("ACME deployment record exceeds its limit")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("invalid ACME deployment record")
    return value


def target(state, values, certificates):
    binding = read(state / BINDING)
    deployment = Path(binding["deployment"])
    if not deployment.is_absolute() or str(deployment.absolute()) != str(deployment):
        raise ValueError("ACME binding requires an absolute deployment path")
    archive.private(deployment, directory=True)
    archive.private(deployment / "deployment.json")
    if archive.digest(deployment / "deployment.json") != binding["configuration"]:
        raise ValueError("bound deployment configuration changed; explicitly bind it again")
    configured = certificates.stack.configuration(
        json.loads((deployment / "deployment.json").read_bytes())
    )
    if any(configured[key] != values[key] for key in ("api_host", "identity_host")):
        raise ValueError("ACME domains differ from the deployment")
    if archive.digest(deployment / "identity-ca.pem") != archive.digest(state / "identity-ca.pem"):
        raise ValueError("automatic delivery cannot change deployment CA trust")
    return deployment, binding


def bind(state, deployment, values, certificates):
    if certificates.present(state / PENDING):
        raise ValueError("finish pending delivery before changing its deployment binding")
    deployment = deployment.absolute()
    with certificates.stack.operation_lock(deployment):
        certificates.stack.require_ready(deployment)
        _, _, configured = certificates.stack.deployment(
            deployment, check=True, verify_certificates=False
        )
        if any(configured[key] != values[key] for key in ("api_host", "identity_host")):
            raise ValueError("ACME domains differ from the deployment")
        if archive.digest(deployment / "identity-ca.pem") != archive.digest(
            state / "identity-ca.pem"
        ):
            raise ValueError("automatic delivery requires the same explicit CA bundle")
        archive.replace_json(
            state / BINDING,
            {
                "version": 1,
                "deployment": str(deployment),
                "configuration": archive.digest(deployment / "deployment.json"),
                "material": certificates.hashes(deployment),
            },
        )


def unbind(state):
    # The disabled record commits before removing intent. A crash can never
    # re-enable a binding whose operator has withdrawn automatic delivery.
    archive.replace_json(state / BINDING, {"version": 1, "disabled": True})
    (state / PENDING).unlink(missing_ok=True)
    archive.sync_directory(state)


def preflight(state, values, certificates):
    if not certificates.present(state / BINDING):
        if certificates.present(state / PENDING):
            raise ValueError("pending certificate delivery has lost its deployment binding")
        return None
    if read(state / BINDING).get("disabled") is True:
        return None
    deployment, binding = target(state, values, certificates)
    with certificates.stack.operation_lock(deployment):
        pending = read(state / PENDING) if certificates.present(state / PENDING) else None
        if pending is not None:
            if (
                pending.get("deployment") != str(deployment)
                or re.fullmatch(r"[a-f0-9]{32}", pending.get("update_id", "")) is None
            ):
                raise ValueError("pending certificate delivery belongs to another deployment")
        marker = deployment / certificates.MARKER
        if certificates.present(marker):
            journal = certificates.read_journal(deployment)
            if pending is None or journal["update_id"] != pending["update_id"]:
                raise ValueError("another certificate installation is pending")
            if journal["replacement"] not in pending["candidates"]:
                raise ValueError("an operator changed this issuer's pending candidate")
            certificates.check_current(deployment, journal)
        else:
            certificates.stack.require_ready(deployment)
            current = certificates.hashes(deployment)
            accepted = (
                [pending["initial"], pending["replacement"]] if pending else [binding["material"]]
            )
            if current not in accepted:
                raise ValueError(
                    "deployment certificates changed outside this issuer; explicitly bind them again"
                )
        for other in certificates.CONFLICTS:
            if certificates.present(deployment / other):
                raise ValueError("another deployment recovery or credential operation is pending")
    return deployment, binding, pending


def deliver(state, bundle, values, certificates):
    ready = preflight(state, values, certificates)
    if ready is None:
        return
    deployment, binding, pending = ready
    replacement = certificates.hashes(bundle)
    if pending is None:
        if replacement == binding["material"]:
            return
        pending = {
            "version": 1,
            "deployment": str(deployment),
            "update_id": uuid.uuid4().hex,
            "initial": binding["material"],
            "replacement": replacement,
            "candidates": [replacement],
        }
        archive.replace_json(state / PENDING, pending)
    elif pending["replacement"] != replacement:
        # An expired pending candidate may have required a new ACME renewal.
        # Preflight confirmed this is still our own operation before advancing it.
        if len(pending["candidates"]) >= 32:
            raise ValueError("repeated delivery failures require operator recovery")
        pending["candidates"].append(replacement)
        pending.update(initial=certificates.hashes(deployment), replacement=replacement)
        archive.replace_json(state / PENDING, pending)
    marker = deployment / certificates.MARKER
    if certificates.present(marker):
        journal = certificates.read_journal(deployment)
        if journal["update_id"] != pending["update_id"]:
            raise ValueError("another certificate installation won the deployment lock")
        action = "resume" if journal["replacement"] == replacement else "replace-pending"
        certificates.install(
            deployment,
            action=action,
            certificates=bundle / "certificates",
            ca_bundle=bundle / "identity-ca.pem",
            minimum_days=0,
            expected_update_id=pending["update_id"],
            expected_replacement=journal["replacement"],
        )
    else:
        receipt = deployment / "certificates.json"
        completed = (
            certificates.present(receipt)
            and read(receipt).get("update_id") == pending["update_id"]
            and certificates.hashes(deployment) == replacement
        )
        if not completed:
            certificates.install(
                deployment,
                certificates=bundle / "certificates",
                ca_bundle=bundle / "identity-ca.pem",
                minimum_days=0,
                update_id=pending["update_id"],
                expected_material=pending["initial"],
            )
    if certificates.hashes(deployment) != replacement:
        raise ValueError("installed certificates differ from the issuer's delivery")
    binding["material"] = replacement
    archive.replace_json(state / BINDING, binding)
    (state / PENDING).unlink()
    archive.sync_directory(state)
