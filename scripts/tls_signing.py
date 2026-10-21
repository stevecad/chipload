"""Resumable Keycloak RS256 rotation, used only by the local credential operator."""

import json
import time
import uuid
from urllib.parse import quote

import jwt

REALM = "/admin/realms/chipload"
PROVIDERS = REALM + "/components"
CERTS = "/realms/chipload/protocol/openid-connect/certs"
TYPE = "org.keycloak.keys.KeyProvider"
RECEIPT = "signing.json"
KINDS = {"signing", "signing-retire"}


def now():
    return int(time.time())


class Rotation:
    def __init__(self, helper, state, values, token):
        self.h, self.state, self.values, self.token = helper, state, values, token

    def api(self, path, *, expected=200, **kwargs):
        return self.h.expect(
            *self.h.request(self.state, self.values, path, token=self.token, admin=True, **kwargs),
            expected,
        )

    def component(self, identifier, *, missing=False):
        status, body = self.h.request(
            self.state,
            self.values,
            PROVIDERS + "/" + quote(identifier, safe=""),
            token=self.token,
            admin=True,
        )
        if status == 404 and missing:
            return None
        return self.h.expect(status, body, 200)

    def keys(self):
        return self.api(REALM + "/keys")

    def key(self, provider):
        matches = [key for key in self.keys()["keys"] if key["providerId"] == provider]
        if len(matches) != 1 or matches[0]["algorithm"] != "RS256":
            raise self.h.CredentialError("expected one RS256 key for the recorded provider")
        return matches[0]

    @staticmethod
    def identity(component):
        # Persist public settings only; never save a private key from an admin response.
        return {
            **{
                key: component[key]
                for key in ("id", "name", "parentId", "providerId", "providerType")
            },
            "config": {
                key: component["config"].get(key) for key in ("priority", "algorithm", "keySize")
            },
        }

    def checked(self, expected, *, missing=False):
        component = self.component(expected["id"], missing=missing)
        if component is not None and self.identity(component) != expected:
            raise self.h.CredentialError("signing provider changed outside this rotation")
        return component

    def set_flags(self, expected, **flags):
        component = self.checked(expected)
        changes = {key: ["true" if value else "false"] for key, value in flags.items()}
        if any(component["config"].get(k, ["true"]) != v for k, v in changes.items()):
            component["config"].update(changes)
            self.api(
                PROVIDERS + "/" + quote(component["id"], safe=""),
                method="PUT",
                data=component,
                expected=204,
            )

    def save(self, journal):
        self.h.archive.replace_json(self.state / self.h.MARKER, journal)

    def receipt(self):
        path = self.state / RECEIPT
        self.h.archive.private(path)
        if path.stat().st_size > 32 * 1024:
            raise self.h.CredentialError("signing receipt exceeds its size limit")
        record = json.loads(path.read_bytes())
        if record.get("version") != 1 or record.get("status") not in {"overlap", "retired"}:
            raise self.h.CredentialError("invalid signing rotation receipt")
        return record

    def prepare(self, kind, retain_days):
        if kind == "signing-retire":
            record = self.receipt()
            if record["status"] != "overlap" or now() < record["retire_after"]:
                raise self.h.CredentialError("previous signing key is not due for retirement")
            self.verify_keys(record, old_required=True)
            if self.checked(record["old"])["config"].get("active", ["true"]) != ["false"]:
                raise self.h.CredentialError("previous signing provider is no longer passive")
            return {"signing": record, "phase": "retire"}
        if type(retain_days) is not int or not 1 <= retain_days <= 365:
            raise self.h.CredentialError("signing retention must be 1–365 days")
        path = self.state / RECEIPT
        if (path.exists() or path.is_symlink()) and self.receipt()["status"] != "retired":
            raise self.h.CredentialError("retire the previous signing key before another rotation")
        realm = self.api(REALM)
        keys = self.keys()
        active = [key for key in keys["keys"] if key["kid"] == keys["active"].get("RS256")]
        if len(active) != 1:
            raise self.h.CredentialError("realm must have one active RS256 signing key")
        old = self.component(active[0]["providerId"])
        if old["providerId"] != "rsa-generated" or old["providerType"] != TYPE:
            raise self.h.CredentialError("rotation requires a generated RSA provider")
        providers = self.api(PROVIDERS + "?type=" + TYPE)
        priority = max(int(p["config"].get("priority", ["0"])[0]) for p in providers) + 1
        if not 0 < priority < 2**31:
            raise self.h.CredentialError("signing provider priority is outside supported bounds")
        identifier = uuid.uuid4().hex
        component = {
            "id": identifier,
            "name": "chipload-signing-" + identifier,
            "parentId": realm["id"],
            "providerId": "rsa-generated",
            "providerType": TYPE,
            "config": {
                "priority": [str(priority)],
                "algorithm": ["RS256"],
                "keySize": ["3072"],
                "active": ["false"],
                "enabled": ["true"],
            },
        }
        jwks = self.h.expect(*self.h.request(self.state, self.values, CERTS), 200)
        if len(jwks["keys"]) >= 32:
            raise self.h.CredentialError("retire unused realm keys before adding another")
        return {
            "phase": "create",
            "component": component,
            "retain_days": retain_days,
            "signing": {
                "version": 1,
                "status": "overlap",
                "old": self.identity(old),
                "old_kid": active[0]["kid"],
                "new": self.identity(component),
            },
        }

    def verify_keys(self, record, *, old_required):
        self.checked(record["new"])
        keys = self.keys()
        if keys["active"].get("RS256") != record["new_kid"]:
            raise self.h.CredentialError("active signing key changed outside this rotation")
        if self.key(record["new"]["id"])["kid"] != record["new_kid"]:
            raise self.h.CredentialError("replacement signing key changed")
        if old_required:
            old = self.checked(record["old"])
            if old["config"].get("enabled", ["true"]) != ["true"]:
                raise self.h.CredentialError("previous signing key was disabled outside rotation")
            if self.key(record["old"]["id"])["kid"] != record["old_kid"]:
                raise self.h.CredentialError("previous signing key changed")

    def verify_agent(self, journal):
        secret = (self.state / "secrets/agent-secret").read_text()
        token = self.h.expect(*self.h.agent_token(self.state, self.values, secret), 200)[
            "access_token"
        ]
        if jwt.get_unverified_header(token).get("kid") != journal["signing"]["new_kid"]:
            raise self.h.CredentialError("fresh agent token did not use the replacement key")
        # Unknown-key refresh is throttled for five seconds in the live API.
        # Retry only this read, never a mutation or an issued operation.
        deadline = time.monotonic() + 8
        while True:
            status, identity = self.h.request(
                self.state, self.values, "/v1/identity", token=token, api=True
            )
            if status == 200:
                if identity != journal["identity"]:
                    raise self.h.CredentialError(
                        "agent identity or grants changed during signing rotation"
                    )
                return
            if status != 401 or time.monotonic() >= deadline:
                self.h.expect(status, identity, 200)
            time.sleep(0.25)

    def rotate(self, journal):
        record = journal["signing"]
        if journal["phase"] not in {"create", "activate", "overlap"}:
            raise self.h.CredentialError("invalid signing rotation phase")
        if journal["phase"] == "create":
            if self.checked(record["new"], missing=True) is None:
                self.api(PROVIDERS, method="POST", data=journal["component"], expected=201)
            self.checked(record["new"])
            record["new_kid"] = self.key(record["new"]["id"])["kid"]
            journal["phase"] = "activate"
            self.save(journal)
        if journal["phase"] == "activate":
            active = self.keys()["active"].get("RS256")
            if active not in {record["old_kid"], record["new_kid"]}:
                raise self.h.CredentialError("active signing key changed outside this rotation")
            # Check both exact key identities before changing signing selection.
            self.checked(record["old"])
            if (
                self.key(record["old"]["id"])["kid"] != record["old_kid"]
                or self.key(record["new"]["id"])["kid"] != record["new_kid"]
            ):
                raise self.h.CredentialError("signing key changed outside this rotation")
            self.set_flags(record["new"], active=True, enabled=True)
            self.verify_keys(record, old_required=True)
            self.verify_agent(journal)
            self.set_flags(record["old"], active=False)
            # Start retention after the old provider is confirmed passive, not
            # before the first remote change. An interrupted reply extends it.
            record["activated_at"] = now()
            record["retire_after"] = record["activated_at"] + journal["retain_days"] * 86400
            journal["phase"] = "overlap"
            self.save(journal)
        self.verify_keys(record, old_required=True)
        published = self.h.expect(*self.h.request(self.state, self.values, CERTS), 200)
        if not {record["old_kid"], record["new_kid"]} <= {key["kid"] for key in published["keys"]}:
            raise self.h.CredentialError("both signing keys must remain published during overlap")
        self.h.archive.replace_json(self.state / RECEIPT, record)

    def retire(self, journal, command, environment):
        record = journal["signing"]
        if journal["phase"] not in {"retire", "disabled", "delete"}:
            raise self.h.CredentialError("invalid signing retirement phase")
        self.verify_keys(record, old_required=False)
        old = self.checked(record["old"], missing=journal["phase"] == "delete")
        if old is not None:
            if self.key(record["old"]["id"])["kid"] != record["old_kid"] or old["config"].get(
                "active", ["true"]
            ) != ["false"]:
                raise self.h.CredentialError("previous signing key changed outside retirement")
            if journal["phase"] != "retire" and old["config"].get("enabled", ["true"]) != ["false"]:
                raise self.h.CredentialError(
                    "previous signing key was re-enabled outside retirement"
                )
        if journal["phase"] == "retire":
            if self.key(record["old"]["id"])["kid"] != record["old_kid"]:
                raise self.h.CredentialError("previous signing key changed")
            self.set_flags(record["old"], active=False, enabled=False)
            journal["phase"] = "disabled"
            self.save(journal)
        if journal["phase"] == "disabled":
            # Recreate the API to discard all retained verification keys.
            self.h.recovery.compose(
                command,
                environment,
                "up",
                "--detach",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                "60",
                "api",
            )
            self.verify_agent(journal)
            published = self.h.expect(*self.h.request(self.state, self.values, CERTS), 200)
            if record["old_kid"] in {key["kid"] for key in published["keys"]}:
                raise self.h.CredentialError("previous signing key is still published")
            journal["phase"] = "delete"
            self.save(journal)
        if old is not None:
            self.api(
                PROVIDERS + "/" + quote(record["old"]["id"], safe=""), method="DELETE", expected=204
            )
        self.verify_keys(record, old_required=False)
        self.verify_agent(journal)
        record.update(status="retired", retired_at=now())
        self.h.archive.replace_json(self.state / RECEIPT, record)
