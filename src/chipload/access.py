"""Application permissions. Identity verification belongs to the transport boundary."""

from typing import Annotated, Literal

from pydantic import Field, StrictStr

from .model import Id, Model

Action = Literal[
    "code:run",
    "code:read",
    "code:cancel",
    "source:read",
    "source:write",
    "build:read",
    "build:run",
    "publication:write",
    "audit:read",
    "evaluation:read",
    "evaluation:run",
    "evidence:read",
    "evidence:revoke",
    "observations:read",
    "observations:write",
    "operation:read",
    "operation:execute",
    "operation:cancel",
]
ACTIONS: tuple[Action, ...] = (
    "code:run",
    "code:read",
    "code:cancel",
    "source:read",
    "source:write",
    "build:read",
    "build:run",
    "publication:write",
    "audit:read",
    "evaluation:read",
    "evaluation:run",
    "evidence:read",
    "evidence:revoke",
    "observations:read",
    "observations:write",
    "operation:read",
    "operation:execute",
    "operation:cancel",
)


class Grant(Model):
    shop_id: Id
    actions: tuple[Action, ...]
    operations: Annotated[tuple[Id, ...], Field(max_length=1000)] | None = None


class Principal(Model):
    subject: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    grants: tuple[Grant, ...] = ()


class Forbidden(PermissionError):
    pass


class AuthenticationError(ValueError):
    pass


class IdentityUnavailable(RuntimeError):
    pass


def require(principal: Principal | None, shop_id: str, action: Action) -> Principal:
    if principal is None or not any(
        grant.shop_id == shop_id and action in grant.actions for grant in principal.grants
    ):
        raise Forbidden("permission denied")
    return principal


def require_operation(
    principal: Principal | None, shop_id: str, operation: str, action: Action
) -> Principal:
    require(principal, shop_id, action)
    if not any(
        grant.shop_id == shop_id
        and action in grant.actions
        and (grant.operations is None or operation in grant.operations)
        for grant in principal.grants
    ):
        raise Forbidden("permission denied")
    return principal
