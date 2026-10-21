"""Exact Keycloak password/OTP flow for the private secrets operator client."""

import uuid

MAX_AGE = 300
FACTORS = (("auth-username-password-form", "pwd"), ("auth-otp-form", "otp"))


class MFAError(ValueError):
    """Fixed diagnostic containing no provider response or credential material."""


def new_flow():
    return str(uuid.uuid4())


def specification(identifier):
    namespace = uuid.UUID(identifier)
    alias = "chipload-secrets-mfa-" + identifier
    flow = {
        "id": identifier, "alias": alias, "providerId": "basic-flow",
        "description": "Chipload secrets operator password and authenticator",
        "topLevel": True, "builtIn": False,
    }
    executions = []
    for index, (provider, factor) in enumerate(FACTORS, 1):
        execution = {
            "id": str(uuid.uuid5(namespace, factor)), "parentFlow": identifier,
            "authenticator": provider, "authenticatorFlow": False,
            "requirement": "REQUIRED", "priority": index * 10,
        }
        configuration = {
            "id": str(uuid.uuid5(namespace, factor + "-config")),
            "alias": alias + "-" + factor,
            "config": {
                "default.reference.value": factor,
                "default.reference.maxAge": str(MAX_AGE),
            },
        }
        executions.append((execution, configuration))
    return flow, executions


def matches(actual, expected):
    return all(actual.get(key) == value for key, value in expected.items())


def ensure(provider, identifier, *, create=False):
    """Resume only the exact journaled flow; never repair a changed execution."""
    flow, specifications = specification(identifier)
    path = "/authentication/flows/" + identifier
    status, actual = provider.admin(path, expected=(200, 404))
    if status == 404 and create:
        if any(row.get("alias") == flow["alias"] for row in provider.admin("/authentication/flows")[1]):
            raise MFAError("operator MFA flow alias belongs to another configuration")
        provider.admin("/authentication/flows", method="POST", data=flow, expected=(201,))
        status, actual = provider.admin(path)
    if status != 200 or not matches(actual, flow):
        raise MFAError("operator MFA flow differs from its receipt")
    listing = "/authentication/flows/" + flow["alias"] + "/executions"
    rows = provider.admin(listing)[1]
    wanted = {execution["id"] for execution, _ in specifications}
    if len(rows) != len({row["id"] for row in rows}) or any(row["id"] not in wanted for row in rows):
        raise MFAError("operator MFA flow contains unexpected executions")
    for execution, configuration in specifications:
        row = next((row for row in rows if row["id"] == execution["id"]), None)
        if row is None and create:
            provider.admin("/authentication/executions", method="POST", data=execution, expected=(201,))
            rows = provider.admin(listing)[1]
            row = next((row for row in rows if row["id"] == execution["id"]), None)
        expected = {
            "id": execution["id"], "providerId": execution["authenticator"],
            "requirement": "REQUIRED", "priority": execution["priority"],
            "level": 0,
        }
        # Keycloak omits authenticationFlow for leaf executions.
        if row is None or not matches(row, expected) or row.get("authenticationFlow") not in (None, False):
            raise MFAError("operator MFA execution differs from its receipt")
        config_id = row.get("authenticationConfig")
        if config_id is None and create:
            provider.admin(
                "/authentication/executions/" + execution["id"] + "/config",
                method="POST", data=configuration, expected=(201,),
            )
            rows = provider.admin(listing)[1]
            row = next(row for row in rows if row["id"] == execution["id"])
            config_id = row.get("authenticationConfig")
        if config_id != configuration["id"]:
            raise MFAError("operator MFA execution configuration differs from its receipt")
        actual = provider.admin("/authentication/config/" + config_id)[1]
        if not matches(actual, configuration):
            raise MFAError("operator MFA references differ from their receipt")
    # Check the complete flow again before binding any client to it.
    rows = provider.admin(listing)[1]
    if [row["id"] for row in rows] != [execution["id"] for execution, _ in specifications]:
        raise MFAError("operator MFA execution order differs from its receipt")


def require_enrolled(provider, subject):
    credentials = provider.admin("/users/" + subject + "/credentials")[1]
    if not any(row.get("type") == "otp" for row in credentials):
        raise MFAError("enroll the operator's authenticator in Keycloak before granting access")
