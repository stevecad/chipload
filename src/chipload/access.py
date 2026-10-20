"""Application permissions. Identity verification belongs to the transport boundary."""

from typing import Annotated, Literal

from pydantic import Field, StrictStr

from .model import Id, Model

Action = Literal[
    "source:read",
    "source:write",
    "build:read",
    "build:run",
    "publication:write",
    "audit:read",
    "evaluation:read",
    "evidence:read",
    "observations:read",
    "observations:write",
]
ACTIONS: tuple[Action, ...] = (
    "source:read",
    "source:write",
    "build:read",
    "build:run",
    "publication:write",
    "audit:read",
    "evaluation:read",
    "evidence:read",
    "observations:read",
    "observations:write",
)


class Grant(Model):
    shop_id: Id
    actions: tuple[Action, ...]


class Principal(Model):
    subject: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    grants: tuple[Grant, ...] = ()


class Forbidden(PermissionError):
    pass


def require(principal: Principal | None, shop_id: str, action: Action) -> Principal:
    if principal is None or not any(
        grant.shop_id == shop_id and action in grant.actions for grant in principal.grants
    ):
        raise Forbidden("permission denied")
    return principal
