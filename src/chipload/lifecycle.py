"""Durable local authoring services with explicit shop/action authorization.

SQLite is a single-node implementation, not shared multi-node storage. Repository
and BuildProcessor are trusted infrastructure; only ShopService is a client API.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from .access import Principal, require
from .artifact import Digest, read_artifact
from .discovery import public_catalog
from .model import Id, Model, Timestamp
from .project import MAX_OUTPUT_BYTES, BuildReport, SourceProject
from .sandbox import DockerBuilder
from .validation import Diagnostic


class Conflict(ValueError):
    pass


class NotFound(LookupError):
    pass


class IntegrityError(ValueError):
    """Stored data failed validation; never present its details as a client error."""


def now() -> datetime:
    return datetime.now(timezone.utc)


class SourceRevision(Model):
    revision: Digest
    project: SourceProject
    created_at: Timestamp


class BuildRecord(Model):
    id: str
    shop_id: Id
    source_revision: Digest
    state: Literal["queued", "running", "succeeded", "failed"]
    created_at: Timestamp
    finished_at: Timestamp | None = None
    deadline: Timestamp | None = None
    report: BuildReport | None = None


class Publication(Model):
    shop_id: Id
    generation: int
    build_id: str | None


SCHEMA = """
CREATE TABLE IF NOT EXISTS shops (
    id TEXT PRIMARY KEY, head TEXT, generation INTEGER NOT NULL DEFAULT 0,
    published_build TEXT
);
CREATE TABLE IF NOT EXISTS sources (
    shop_id TEXT NOT NULL REFERENCES shops(id), revision TEXT NOT NULL,
    project BLOB NOT NULL, created_at TEXT NOT NULL, actor TEXT NOT NULL,
    PRIMARY KEY(shop_id, revision)
);
CREATE TABLE IF NOT EXISTS builds (
    id TEXT PRIMARY KEY, shop_id TEXT NOT NULL, source_revision TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed')),
    created_at TEXT NOT NULL, finished_at TEXT, deadline TEXT, claim TEXT,
    report BLOB,
    FOREIGN KEY(shop_id, source_revision) REFERENCES sources(shop_id, revision)
);
CREATE INDEX IF NOT EXISTS builds_by_shop ON builds(shop_id, created_at, id);
CREATE TABLE IF NOT EXISTS publications (
    shop_id TEXT NOT NULL REFERENCES shops(id), generation INTEGER NOT NULL,
    build_id TEXT REFERENCES builds(id), action TEXT NOT NULL,
    actor TEXT NOT NULL, time TEXT NOT NULL,
    PRIMARY KEY(shop_id, generation)
);
CREATE TABLE IF NOT EXISTS audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT, shop_id TEXT NOT NULL REFERENCES shops(id),
    actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observation_heads (
    shop_id TEXT PRIMARY KEY REFERENCES shops(id), revision TEXT NOT NULL,
    observations BLOB NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS build_requests (
    shop_id TEXT NOT NULL REFERENCES shops(id), actor TEXT NOT NULL,
    request_key TEXT NOT NULL, source_revision TEXT NOT NULL,
    build_id TEXT NOT NULL REFERENCES builds(id),
    PRIMARY KEY(shop_id, actor, request_key)
);
"""


class Repository:
    def __init__(self, path: Path):
        self.path = Path(path).absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The operator selects this path. Refuse symlink databases and permissive files.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_mode & 0o077:
                raise ValueError("repository database must be private to its owner (mode 0600)")
        finally:
            os.close(fd)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1, 2}:
                raise ValueError("unsupported repository schema")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.execute("PRAGMA user_version=2")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    @staticmethod
    def event(db, shop: str, actor: str, action: str, target: str):
        db.execute(
            "INSERT INTO audit(shop_id,actor,action,target,time) VALUES(?,?,?,?,?)",
            (shop, actor, action, target, now().isoformat()),
        )

    @staticmethod
    def source(db, shop: str, revision: str | None) -> SourceRevision:
        if revision is None:
            row = db.execute("SELECT head FROM shops WHERE id=?", (shop,)).fetchone()
            revision = row["head"] if row else None
        row = db.execute(
            "SELECT * FROM sources WHERE shop_id=? AND revision=?", (shop, revision)
        ).fetchone()
        if row is None:
            raise NotFound("source revision not found")
        project = SourceProject.model_validate_json(row["project"])
        if project.revision != revision or project.shop_id != shop:
            raise ValueError("stored source integrity check failed")
        return SourceRevision(revision=revision, project=project, created_at=row["created_at"])

    @staticmethod
    def build_record(row) -> BuildRecord:
        try:
            return Repository._decode_build(row)
        except ValueError as error:
            raise IntegrityError(str(error)) from error

    @staticmethod
    def _decode_build(row) -> BuildRecord:
        if row is None:
            raise NotFound("build not found")
        report = BuildReport.model_validate_json(row["report"]) if row["report"] else None
        if report:
            if report.source_revision != row["source_revision"] or report.status != row["state"]:
                raise ValueError("stored build integrity check failed")
            if report.artifact:
                read_artifact(report.artifact.to_json())
                if report.artifact.shop.id != row["shop_id"]:
                    raise ValueError("stored artifact shop does not match")
        return BuildRecord(
            id=row["id"],
            shop_id=row["shop_id"],
            source_revision=row["source_revision"],
            state=row["state"],
            created_at=row["created_at"],
            finished_at=row["finished_at"],
            deadline=row["deadline"],
            report=report,
        )

    def claim(self, build_id: str, seconds: float) -> tuple[str, SourceProject]:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM builds WHERE id=?", (build_id,)).fetchone()
            record = self.build_record(row)
            if record.state != "queued":
                raise Conflict("build has already been claimed")
            token = uuid.uuid4().hex
            deadline = now() + timedelta(seconds=seconds)
            db.execute(
                "UPDATE builds SET state='running',claim=?,deadline=? WHERE id=?",
                (token, deadline.isoformat(), build_id),
            )
            self.event(db, record.shop_id, "worker", "build.started", build_id)
            return token, self.source(db, record.shop_id, record.source_revision).project

    def finish(self, build_id: str, claim: str, report: BuildReport) -> None:
        report = BuildReport.model_validate(report.model_dump(mode="python"))
        payload = report.model_dump_json().encode()
        if len(payload) > MAX_OUTPUT_BYTES:
            raise ValueError("build report exceeds the storage limit")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM builds WHERE id=?", (build_id,)).fetchone()
            record = self.build_record(row)
            if (
                record.state != "running"
                or row["claim"] != claim
                or record.deadline is None
                or record.deadline <= now()
            ):
                raise Conflict("build claim is no longer current")
            if report.source_revision != record.source_revision:
                raise ValueError("build report source does not match")
            if report.artifact:
                read_artifact(report.artifact.to_json())
                if report.artifact.shop.id != record.shop_id:
                    raise ValueError("build artifact shop does not match")
            source = self.source(db, record.shop_id, record.source_revision).project
            if report.status == "succeeded" and (
                tuple(t.id for t in report.tests) != tuple(t.id for t in source.tests)
            ):
                raise ValueError("build report does not cover the project's tests")
            db.execute(
                "UPDATE builds SET state=?,report=?,finished_at=?,claim=NULL WHERE id=?",
                (report.status, payload, now().isoformat(), build_id),
            )
            self.event(db, record.shop_id, "worker", f"build.{report.status}", build_id)

    def fence_expired(self, build_id: str) -> str:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM builds WHERE id=?", (build_id,)).fetchone()
            record = self.build_record(row)
            if record.state != "running" or record.deadline is None or record.deadline > now():
                raise Conflict("build is not expired")
            token = uuid.uuid4().hex
            db.execute(
                "UPDATE builds SET claim=?,deadline=? WHERE id=?",
                (token, (now() + timedelta(seconds=30)).isoformat(), build_id),
            )
            self.event(db, record.shop_id, "worker", "build.recovering", build_id)
            return token


class ShopService:
    def __init__(self, repository: Repository):
        self.repository = repository

    def save_source(
        self, principal: Principal, project: SourceProject, *, expected_head: str | None
    ) -> SourceRevision:
        require(principal, project.shop_id, "source:write")
        project = SourceProject.model_validate(project.model_dump(mode="python"))
        revision = project.revision
        with self.repository.transaction() as db:
            row = db.execute("SELECT head FROM shops WHERE id=?", (project.shop_id,)).fetchone()
            if (row["head"] if row else None) != expected_head:
                raise Conflict("source head changed; read it before saving again")
            if row is None:
                db.execute("INSERT INTO shops(id) VALUES(?)", (project.shop_id,))
            db.execute(
                "INSERT OR IGNORE INTO sources VALUES(?,?,?,?,?)",
                (
                    project.shop_id,
                    revision,
                    project.to_json(),
                    now().isoformat(),
                    principal.subject,
                ),
            )
            db.execute("UPDATE shops SET head=? WHERE id=?", (revision, project.shop_id))
            self.repository.event(db, project.shop_id, principal.subject, "source.saved", revision)
            return self.repository.source(db, project.shop_id, revision)

    def get_source(
        self, principal: Principal, shop: str, revision: str | None = None
    ) -> SourceRevision:
        require(principal, shop, "source:read")
        with self.repository.connection() as db:
            return self.repository.source(db, shop, revision)

    def request_build(
        self, principal: Principal, shop: str, revision: str, *, request_key: str | None = None
    ) -> BuildRecord:
        require(principal, shop, "build:run")
        if request_key is not None and not 1 <= len(request_key) <= 100:
            raise ValueError("invalid build request key")
        with self.repository.transaction() as db:
            self.repository.source(db, shop, revision)
            if request_key is not None:
                prior = db.execute(
                    "SELECT * FROM build_requests WHERE shop_id=? AND actor=? AND request_key=?",
                    (shop, principal.subject, request_key),
                ).fetchone()
                if prior:
                    if prior["source_revision"] != revision:
                        raise Conflict("request key was already used for a different source")
                    return self.repository.build_record(
                        db.execute(
                            "SELECT * FROM builds WHERE id=?", (prior["build_id"],)
                        ).fetchone()
                    )
            build_id = uuid.uuid4().hex
            pending = db.execute(
                "SELECT COUNT(*) FROM builds WHERE shop_id=? AND state IN ('queued','running')",
                (shop,),
            ).fetchone()[0]
            if pending >= 10:
                raise Conflict("shop already has ten pending builds")
            db.execute(
                "INSERT INTO builds(id,shop_id,source_revision,state,created_at) "
                "VALUES(?,?,?,'queued',?)",
                (build_id, shop, revision, now().isoformat()),
            )
            self.repository.event(db, shop, principal.subject, "build.requested", build_id)
            if request_key is not None:
                db.execute(
                    "INSERT INTO build_requests VALUES(?,?,?,?,?)",
                    (shop, principal.subject, request_key, revision, build_id),
                )
            return self.repository.build_record(
                db.execute("SELECT * FROM builds WHERE id=?", (build_id,)).fetchone()
            )

    def get_build(self, principal: Principal, shop: str, build_id: str) -> BuildRecord:
        require(principal, shop, "build:read")
        with self.repository.connection() as db:
            return self.repository.build_record(
                db.execute(
                    "SELECT * FROM builds WHERE id=? AND shop_id=?", (build_id, shop)
                ).fetchone()
            )

    def get_publication(self, principal: Principal, shop: str) -> Publication:
        require(principal, shop, "publication:write")
        with self.repository.connection() as db:
            row = db.execute("SELECT * FROM shops WHERE id=?", (shop,)).fetchone()
            if row is None:
                raise NotFound("shop not found")
            return Publication(
                shop_id=shop, generation=row["generation"], build_id=row["published_build"]
            )

    def publish(
        self, principal: Principal, shop: str, build_id: str, *, expected_generation: int
    ) -> Publication:
        return self._publication(principal, shop, build_id, expected_generation, "publish")

    def rollback(
        self, principal: Principal, shop: str, build_id: str, *, expected_generation: int
    ) -> Publication:
        return self._publication(principal, shop, build_id, expected_generation, "rollback")

    def withdraw(self, principal: Principal, shop: str, *, expected_generation: int) -> Publication:
        return self._publication(principal, shop, None, expected_generation, "withdraw")

    def _publication(
        self,
        principal: Principal,
        shop: str,
        build_id: str | None,
        expected_generation: int,
        action: str,
    ) -> Publication:
        require(principal, shop, "publication:write")
        with self.repository.transaction() as db:
            row = db.execute("SELECT * FROM shops WHERE id=?", (shop,)).fetchone()
            if row is None:
                raise NotFound("shop not found")
            if row["generation"] != expected_generation:
                raise Conflict("publication changed; read its generation before trying again")
            if build_id is not None:
                build = self.repository.build_record(
                    db.execute(
                        "SELECT * FROM builds WHERE id=? AND shop_id=?", (build_id, shop)
                    ).fetchone()
                )
                if build.state != "succeeded" or not build.report or not build.report.tests:
                    raise Conflict("publication requires a successful build with acceptance tests")
                if (
                    action == "rollback"
                    and not db.execute(
                        "SELECT 1 FROM publications WHERE shop_id=? AND build_id=?",
                        (shop, build_id),
                    ).fetchone()
                ):
                    raise Conflict("rollback target has never been published")
            generation = row["generation"] + 1
            db.execute(
                "UPDATE shops SET generation=?,published_build=? WHERE id=?",
                (generation, build_id, shop),
            )
            db.execute(
                "INSERT INTO publications VALUES(?,?,?,?,?,?)",
                (shop, generation, build_id, action, principal.subject, now().isoformat()),
            )
            self.repository.event(
                db, shop, principal.subject, f"publication.{action}", str(generation)
            )
            return Publication(shop_id=shop, generation=generation, build_id=build_id)

    def catalog(self, shop: str) -> dict:
        # One snapshot selects exactly the published build. There is no process cache.
        with self.repository.connection() as db:
            row = db.execute(
                "SELECT builds.* FROM shops JOIN builds ON shops.published_build=builds.id "
                "WHERE shops.id=?",
                (shop,),
            ).fetchone()
            if row is None:
                raise NotFound("published shop not found")
            record = self.repository.build_record(row)
            if record.state != "succeeded" or record.report is None:
                raise ValueError("invalid publication")
            try:
                return public_catalog(record.report.artifact)
            except KeyError:
                raise NotFound("published shop not found") from None

    def audit(
        self, principal: Principal, shop: str, *, after: int = 0, limit: int = 100
    ) -> list[dict]:
        require(principal, shop, "audit:read")
        if not 1 <= limit <= 100 or after < 0:
            raise ValueError("invalid audit page")
        with self.repository.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT sequence,actor,action,target,time FROM audit "
                    "WHERE shop_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                    (shop, after, limit),
                )
            ]


class BuildProcessor:
    """Trusted broker API. Never expose this object's methods as client tools."""

    def __init__(self, repository: Repository, builder: DockerBuilder):
        self.repository = repository
        self.builder = builder

    def run(self, build_id: str) -> None:
        claim, project = self.repository.claim(build_id, self.builder.seconds + 60)
        try:
            report = self.builder.build(project, job_id=build_id)
            if len(report.model_dump_json().encode()) > MAX_OUTPUT_BYTES:
                raise ValueError("build report exceeds the storage limit")
        except Exception:
            logging.getLogger(__name__).exception("Build broker failed for job %s", build_id)
            # Keep internal exception details out of serialized service errors.
            report = BuildReport(
                source_revision=project.revision,
                worker_image=self.builder.environment.image,
                status="failed",
                diagnostics=(
                    Diagnostic(
                        code="broker-error",
                        path=(),
                        message="Build broker failed; inspect operator logs.",
                    ),
                ),
            )
        self.repository.finish(build_id, claim, report)

    def recover_expired(self) -> int:
        """Fence expired claims and remove their containers before recording failure."""
        count = 0
        with self.repository.connection() as db:
            expired = db.execute(
                "SELECT * FROM builds WHERE state='running' AND deadline<=?",
                (now().isoformat(),),
            ).fetchall()
        for row in expired:
            try:
                claim = self.repository.fence_expired(row["id"])
            except Conflict:
                continue
            self.builder.remove(row["id"])
            report = BuildReport(
                source_revision=row["source_revision"],
                worker_image=self.builder.environment.image,
                status="failed",
                diagnostics=(
                    Diagnostic(
                        code="worker-interrupted",
                        path=(),
                        message="Build lease expired; its worker was removed.",
                    ),
                ),
            )
            try:
                self.repository.finish(row["id"], claim, report)
                count += 1
            except Conflict:
                pass
        return count
