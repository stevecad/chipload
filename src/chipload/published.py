"""Shared public/private query policy for every transport."""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Annotated, Callable, Literal

from pydantic import Field

from .access import Principal, require
from .artifact import Artifact, Digest, canonical_json, digest
from .evaluate import EvaluationRequest, evaluate
from .evidence import RevocationSnapshot
from .evidence import snapshot as evidence_snapshot
from .lifecycle import Conflict, NotFound, ShopService, now
from .model import Model, Observation, Timestamp
from .project import unique_json
from .query import VariableQuery, query_variables

Collection = Literal[
    "sites", "resources", "catalogs", "processes", "capabilities", "workflows", "offers", "variables", "operations"
]


class ObservationSnapshot(Model):
    revision: Digest | None
    updated_at: Timestamp | None
    observations: Annotated[tuple[Observation, ...], Field(max_length=1000)] = ()


PUBLIC_REASONS = {
    "model-supports-request": "The published capability and applicable evidence support these inputs.",
    "outside-range": "An input is outside a published capability limit.",
    "invalid-increment": "An input does not meet a published quantity increment.",
    "value-not-supported": "An input does not meet a modeled requirement.",
    "missing-fact": "A required input is missing.",
    "incompatible-units": "An input uses incompatible measurement units.",
    "unmodeled-requirement": "The model cannot establish the requested additional requirement.",
    "incompatible-input-state": "The requested state is incompatible with this process or workflow.",
    "unpublished-condition": "Some necessary conditions have not been published for evaluation.",
    "alternative-supported": "At least one complete route supports these inputs.",
    "alternative-conditional": "At least one complete route has explicit conditions to resolve.",
    "alternative-unknown": "No route establishes support; at least one needs more information.",
    "alternative-unsupported": "Every declared route rejects these inputs.",
}


class PublishedService:
    def __init__(self, service: ShopService, *, clock: Callable[[], datetime] = now):
        self.service = service
        self.repository = service.repository
        self.clock = clock

    @staticmethod
    def _observations(row) -> ObservationSnapshot:
        if row is None:
            return ObservationSnapshot(revision=None, updated_at=None)
        data = bytes(row["observations"])
        if digest(data) != row["revision"]:
            raise ValueError("stored observation integrity check failed")
        return ObservationSnapshot(
            revision=row["revision"],
            updated_at=row["updated_at"],
            observations=unique_json(data)["observations"],
        )

    def _snapshot(self, shop: str) -> tuple[Artifact, ObservationSnapshot, RevocationSnapshot]:
        with self.repository.connection() as db:
            # Publication, observations and evidence revocations share one read snapshot.
            db.execute("BEGIN")
            row = db.execute(
                "SELECT builds.* FROM shops JOIN builds ON shops.published_build=builds.id "
                "WHERE shops.id=? AND builds.shop_id=shops.id",
                (shop,),
            ).fetchone()
            if row is None:
                raise NotFound("published shop not found")
            build = self.repository.build_record(row)
            if build.state != "succeeded" or build.report is None or build.report.artifact is None:
                raise ValueError("invalid publication")
            observations = self._observations(
                db.execute("SELECT * FROM observation_heads WHERE shop_id=?", (shop,)).fetchone()
            )
            artifact = build.report.artifact
            return artifact, observations, evidence_snapshot(db, artifact)

    def catalog(self, shop: str) -> dict:
        return self.service.catalog(shop)

    def page(
        self,
        shop: str,
        collection: Collection,
        *,
        limit: int = 50,
        cursor: str | None = None,
        query: str = "",
    ) -> dict:
        if not 1 <= limit <= 100 or len(query) > 100:
            raise ValueError("invalid page request")
        catalog = self.catalog(shop)
        identity = {
            "shop": shop,
            "collection": collection,
            "query": query,
            "revision": catalog["public_sha256"],
        }
        after = ""
        if cursor:
            try:
                if len(cursor) > 2048:
                    raise ValueError()
                data = unique_json(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if not isinstance(data, dict) or set(data) != {*identity, "after"}:
                    raise ValueError()
                if any(data[k] != identity[k] for k in ("shop", "collection", "query")):
                    raise ValueError()
                if data["revision"] != identity["revision"]:
                    raise Conflict("published revision changed; restart pagination")
                after = data["after"]
                if not isinstance(after, str) or len(after) > 64:
                    raise ValueError()
            except (ValueError, KeyError, TypeError) as error:
                if isinstance(error, Conflict):
                    raise
                raise ValueError("invalid page cursor") from None
        records = [
            item
            for item in sorted(catalog.get(collection, ()), key=lambda item: item["id"])
            if item["id"] > after
            and query.casefold() in (item["id"] + " " + item["name"]).casefold()
        ]
        page = records[:limit]
        following = None
        if len(records) > limit:
            following = base64.urlsafe_b64encode(
                canonical_json({**identity, "after": page[-1]["id"]})
            ).decode()
        return {"revision": identity["revision"], "items": page, "next_cursor": following}

    def item(self, shop: str, collection: Collection, item_id: str) -> dict:
        catalog = self.catalog(shop)
        item = next((item for item in catalog.get(collection, ()) if item["id"] == item_id), None)
        if item is None:
            raise NotFound("published item not found")
        return {"revision": catalog["public_sha256"], "item": item}

    def variables(self, shop: str, query: VariableQuery, *, principal=None, private=False) -> dict:
        if private:
            require(principal, shop, "evaluation:read")
        artifact, _, _ = self._snapshot(shop)
        try:
            return query_variables(
                artifact,
                query,
                at=self.clock(),
                public_only=not private,
                _public_catalog=self.repository.public_catalog,
            ).model_dump(mode="json")
        except KeyError:
            raise NotFound("variable not found") from None

    def evaluation(
        self, shop: str, query: EvaluationRequest, *, principal=None, private=False
    ) -> dict:
        if private:
            require(principal, shop, "evaluation:read")
        artifact, observations, revocations = self._snapshot(shop)
        at = self.clock()
        if private:
            try:
                result = evaluate(artifact, query, at=at, observations=observations.observations,
                                  revoked_evidence=revocations.ids)
            except KeyError:
                raise NotFound("evaluation target not found") from None
            return {"observation_revision": observations.revision,
                    "revocation_revision": revocations.revision, **result.model_dump(mode="json")}
        try:
            catalog = self.repository.public_catalog(artifact)
        except KeyError:
            raise NotFound("published shop not found") from None
        collection = {"capability": "capabilities", "workflow": "workflows", "offer": "offers"}[query.target_kind]
        target = next((c for c in catalog.get(collection, ()) if c["id"] == query.target), None)
        if target is None:
            raise NotFound("evaluation target not found")
        capabilities = {cap["id"]: cap for cap in catalog["capabilities"]}
        workflows = {workflow["id"]: workflow for workflow in catalog["workflows"]}

        def conditions(kind, item):
            result = list(item["requirements"])
            if kind == "workflow":
                for identifier in item["capabilities"]:
                    result.extend(capabilities[identifier]["requirements"])
            elif kind == "offer":
                for route in item["alternatives"]:
                    targets = capabilities if route["target_kind"] == "capability" else workflows
                    result.extend(conditions(route["target_kind"], targets[route["target"]]))
            return result

        requirements = conditions(query.target_kind, target)
        # Do not test guessed values against hidden catalog membership or private steps.
        opaque = target.get("complete") is False or any(
            rule.get("complete") is False for rule in requirements
        )
        common = {
            "target_kind": query.target_kind,
            "target": query.target,
            "revision": catalog["public_sha256"],
            "evaluated_at": at.isoformat(),
        }
        if opaque:
            return {
                **common,
                "outcome": "unknown",
                "simulated": False,
                "freshness": [],
                "reasons": [
                    {
                        "code": "unpublished-condition",
                        "message": PUBLIC_REASONS["unpublished-condition"],
                    }
                ],
            }
        result = evaluate(artifact, query, at=at, observations=observations.observations,
                          revoked_evidence=revocations.ids)
        codes = sorted({reason.code for reason in result.reasons})
        response = {
            **common,
            "outcome": result.outcome,
            "simulated": result.simulated,
            "reasons": [
                {
                    "code": code,
                    "message": PUBLIC_REASONS.get(
                        code,
                        "A required resource, observation, or evidence condition needs attention.",
                    ),
                }
                for code in codes
            ],
            # Publication opts into this capability's outcome and freshness summary.
            # Private identifiers, values, reason text, paths, and step traces stay private.
            "freshness": [
                use.model_dump(mode="json", exclude={"id"}) for use in result.observations
            ],
        }
        if result.alternatives:
            response["alternatives"] = [
                {"target_kind": route.target_kind, "target": route.target,
                 "outcome": route.decision.outcome, "simulated": route.simulated}
                for route in result.alternatives
            ]
        if result.matching_alternative:
            response["matching_alternative"] = result.matching_alternative.model_dump(mode="json")
        return response

    def read_evidence(self, principal: Principal, shop: str) -> dict:
        require(principal, shop, "evidence:read")
        artifact, _, revocations = self._snapshot(shop)
        return {
            "revision": artifact.model_sha256,
            "evidence": [record.model_dump(mode="json") for record in artifact.shop.evidence],
            "revocation_revision": revocations.revision,
            "revocations": [record.model_dump(mode="json", include={"evidence_id", "revoked_at"})
                            for record in revocations.records],
        }

    def read_observations(self, principal: Principal, shop: str) -> ObservationSnapshot:
        require(principal, shop, "observations:read")
        _, observations, _ = self._snapshot(shop)
        return observations

    def write_observations(
        self,
        principal: Principal,
        shop: str,
        observations: tuple[Observation, ...],
        *,
        expected_revision: str | None,
    ) -> ObservationSnapshot:
        require(principal, shop, "observations:write")
        snapshot = ObservationSnapshot(revision=None, updated_at=None, observations=observations)
        if len({o.id for o in observations}) != len(observations):
            raise ValueError("observation IDs must be unique")
        payload = canonical_json(
            {"observations": [o.model_dump(mode="json") for o in snapshot.observations]}
        )
        with self.repository.transaction() as db:
            row = db.execute(
                "SELECT builds.* FROM shops JOIN builds ON shops.published_build=builds.id "
                "WHERE shops.id=? AND builds.shop_id=shops.id",
                (shop,),
            ).fetchone()
            build = self.repository.build_record(row)
            resources = {r.id for r in build.report.artifact.shop.resources}
            if any(o.resource not in resources for o in observations):
                raise ValueError("observation resource is not in the published shop")
            previous = db.execute(
                "SELECT revision FROM observation_heads WHERE shop_id=?", (shop,)
            ).fetchone()
            if (previous["revision"] if previous else None) != expected_revision:
                raise Conflict("observations changed; read their revision before writing again")
            timestamp = self.clock().isoformat()
            db.execute(
                "INSERT INTO observation_heads VALUES(?,?,?,?) ON CONFLICT(shop_id) DO UPDATE SET "
                "revision=excluded.revision,observations=excluded.observations,updated_at=excluded.updated_at",
                (shop, digest(payload), payload, timestamp),
            )
            self.repository.event(
                db, shop, principal.subject, "observations.updated", digest(payload)
            )
            return ObservationSnapshot(
                revision=digest(payload), updated_at=timestamp, observations=observations
            )
