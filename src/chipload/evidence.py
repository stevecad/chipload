"""Durable shop-owned evidence revocation, independent of source publication."""

from typing import Annotated

from pydantic import Field, StrictStr

from .access import require
from .artifact import Digest, canonical_json, digest
from .limits import bounded_json
from .model import Id, Model, Timestamp
from .quotas import QuotaExceeded

MAX_REVOCATIONS_PER_SHOP = 10_000
MAX_REVOCATION_BYTES = 16_384

EVIDENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence_revocations (
    shop_id TEXT NOT NULL REFERENCES shops(id), evidence_id TEXT NOT NULL,
    record BLOB NOT NULL, sha256 TEXT NOT NULL,
    PRIMARY KEY(shop_id,evidence_id)
);
"""


class EvidenceReference(Model):
    shop_id: Id
    evidence_id: Id


class RevokeEvidence(EvidenceReference):
    expected_model_sha256: Digest
    reason: Annotated[StrictStr, Field(min_length=1, max_length=1000)]


class EvidenceRevocation(EvidenceReference):
    model_sha256: Digest
    reason: Annotated[StrictStr, Field(min_length=1, max_length=1000)]
    actor: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    revoked_at: Timestamp


class RevocationSnapshot(Model):
    """Only records for evidence in the selected artifact; never publicly exposed."""

    records: tuple[EvidenceRevocation, ...] = Field(default=(), max_length=1000)

    @property
    def ids(self) -> frozenset[str]:
        return frozenset(record.evidence_id for record in self.records)

    @property
    def revision(self) -> str | None:
        return digest(canonical_json(self)) if self.records else None


def read_record(row):
    from .lifecycle import IntegrityError, NotFound

    if row is None:
        raise NotFound("evidence revocation not found")
    try:
        payload = bytes(row["record"])
        if len(payload) > MAX_REVOCATION_BYTES or digest(payload) != row["sha256"]:
            raise ValueError("digest mismatch")
        record = EvidenceRevocation.model_validate(
            bounded_json(payload, max_bytes=MAX_REVOCATION_BYTES)
        )
        if (record.shop_id, record.evidence_id) != (row["shop_id"], row["evidence_id"]):
            raise ValueError("identity mismatch")
        return record
    except ValueError as error:
        raise IntegrityError("stored evidence revocation failed validation") from error


def snapshot(db, artifact) -> RevocationSnapshot:
    # Query at most 1000 known identities under the caller's publication snapshot.
    # Old evidence may remain revoked, but its history never expands query output.
    identifiers = sorted(record.id for record in artifact.shop.evidence)
    records = []
    for offset in range(0, len(identifiers), 200):
        batch = identifiers[offset : offset + 200]
        marks = ",".join("?" for _ in batch)
        rows = db.execute(
            "SELECT * FROM evidence_revocations WHERE shop_id=? "
            f"AND evidence_id IN ({marks}) ORDER BY evidence_id",
            (artifact.shop.id, *batch),
        )
        records.extend(read_record(row) for row in rows)
    return RevocationSnapshot(records=tuple(records))


class EvidenceService:
    def __init__(self, repository, *, clock):
        self.repository, self.clock = repository, clock

    def read(self, principal, reference: EvidenceReference) -> EvidenceRevocation:
        require(principal, reference.shop_id, "evidence:read")
        with self.repository.connection() as db:
            return read_record(
                db.execute(
                    "SELECT * FROM evidence_revocations WHERE shop_id=? AND evidence_id=?",
                    (reference.shop_id, reference.evidence_id),
                ).fetchone()
            )

    def revoke(self, principal, request: RevokeEvidence) -> EvidenceRevocation:
        from .lifecycle import Conflict, IntegrityError, NotFound

        require(principal, request.shop_id, "evidence:revoke")
        # Validate direct SDK calls too, before acquiring a writer lock.
        request = RevokeEvidence.model_validate(request.model_dump())
        with self.repository.transaction() as db:
            previous = db.execute(
                "SELECT * FROM evidence_revocations WHERE shop_id=? AND evidence_id=?",
                (request.shop_id, request.evidence_id),
            ).fetchone()
            if previous is not None:
                record = read_record(previous)
                if (record.model_sha256, record.reason, record.actor) != (
                    request.expected_model_sha256,
                    request.reason,
                    principal.subject,
                ):
                    raise Conflict("evidence is already revoked; inspect its existing receipt")
                return record  # Exact retry remains valid after withdrawal or rollback.
            row = db.execute(
                "SELECT builds.* FROM shops JOIN builds ON shops.published_build=builds.id "
                "WHERE shops.id=? AND builds.shop_id=shops.id",
                (request.shop_id,),
            ).fetchone()
            if row is None:
                raise NotFound("published shop not found")
            build = self.repository.build_record(row)
            if build.state != "succeeded" or build.report is None or build.report.artifact is None:
                raise IntegrityError("invalid evidence publication")
            artifact = build.report.artifact
            if artifact.model_sha256 != request.expected_model_sha256:
                raise Conflict("published model changed; inspect its evidence before revoking")
            if not any(proof.id == request.evidence_id for proof in artifact.shop.evidence):
                raise NotFound("published evidence not found")
            count = db.execute(
                "SELECT COUNT(*) FROM evidence_revocations WHERE shop_id=?",
                (request.shop_id,),
            ).fetchone()[0]
            if count >= MAX_REVOCATIONS_PER_SHOP:
                raise QuotaExceeded("retained_evidence_revocations")
            record = EvidenceRevocation(
                shop_id=request.shop_id,
                evidence_id=request.evidence_id,
                model_sha256=artifact.model_sha256,
                reason=request.reason,
                actor=principal.subject,
                revoked_at=self.clock(),
            )
            payload = canonical_json(record)
            if len(payload) > MAX_REVOCATION_BYTES:
                raise ValueError("evidence revocation exceeds the byte limit")
            db.execute(
                "INSERT INTO evidence_revocations VALUES(?,?,?,?)",
                (record.shop_id, record.evidence_id, payload, digest(payload)),
            )
            self.repository.event(
                db, record.shop_id, principal.subject, "evidence.revoked", record.evidence_id
            )
            return record
