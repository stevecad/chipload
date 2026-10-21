"""Durable local authoring services with explicit shop/action authorization.

SQLite is a single-node implementation, not shared multi-node storage. Repository
and BuildProcessor are trusted infrastructure; only ShopService is a client API.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from .access import Principal, require
from .admission import AdmissionStore, PolicyConflict
from .artifact import Artifact, Digest, canonical_json, digest, read_artifact
from .cache import DEFAULT_CACHE_BYTES, DataCache
from .discovery import public_catalog
from .execution_model import EXECUTION_SCHEMA
from .limits import bounded_json
from .model import Id, Model, Timestamp
from .program_model import PROGRAM_SCHEMA
from .project import MAX_OUTPUT_BYTES, MAX_PROJECT_BYTES, BuildReport, SourceProject
from .quotas import QUOTA_SCHEMA, QuotaPolicy, admit_build, admit_running, admit_source, quota_usage
from .request_limits import REQUEST_SCHEMA
from .sandbox import DockerBuilder
from .storage import private_database
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
CREATE INDEX IF NOT EXISTS audit_build_origin ON audit(action,target,shop_id,sequence);
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
    def __init__(self, path: Path, *, cache_bytes: int = DEFAULT_CACHE_BYTES):
        self.data_cache = DataCache(max_bytes=cache_bytes)
        self.path = Path(path).absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The operator selects this path. Refuse symlink databases and permissive files.
        private_database(self.path, create=True)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1, 2, 3, 4, 5, 6, 7}:
                raise ValueError("unsupported repository schema")
            if version in {5, 6, 7}:
                reference = db.execute("SELECT * FROM admission_store WHERE id=1").fetchone()
                if reference is None:
                    raise IntegrityError("missing admission store reference")
                if reference["state"] == "ready":
                    if db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                        raise IntegrityError("repository database requires WAL journaling")
                    self.admission = self._admission(reference["identity"])
                    self.admission.enable_wal()
                    self.admission.snapshot()
                    if version == 5:
                        self._initialize_executions()
                    if version < 7:
                        self._initialize_programs()
                    return
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            try:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in {0, 1, 2, 3, 4, 5, 6, 7}:
                    raise ValueError("unsupported repository schema")
                if version in {5, 6, 7}:
                    db.execute("COMMIT")
                    self._initialize_admission()
                    return
                # execute(), rather than executescript(), preserves this migration's transaction.
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        db.execute(statement)
                columns = {row["name"] for row in db.execute("PRAGMA table_info(builds)")}
                if "actor" not in columns:
                    db.execute("ALTER TABLE builds ADD COLUMN actor TEXT NOT NULL DEFAULT 'legacy'")
                    db.execute(
                        "UPDATE builds SET actor=COALESCE("
                        "(SELECT actor FROM audit WHERE action='build.requested' "
                        "AND target=builds.id AND shop_id=builds.shop_id ORDER BY sequence LIMIT 1),"
                        "(SELECT actor FROM sources WHERE shop_id=builds.shop_id "
                        "AND revision=builds.source_revision),'legacy')"
                    )
                for statement in (QUOTA_SCHEMA + REQUEST_SCHEMA).split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute(
                    "INSERT OR IGNORE INTO quota_policy VALUES(1,0,?)",
                    (canonical_json(QuotaPolicy()),),
                )
                db.execute(
                    "INSERT OR IGNORE INTO quota_changes "
                    "SELECT generation,policy,? FROM quota_policy WHERE id=1",
                    (now().isoformat(),),
                )
                db.execute("INSERT OR IGNORE INTO dispatch_clock VALUES(1,0)")
                for scope, column in (("shop", "shop_id"), ("identity", "actor")):
                    db.execute(
                        "INSERT OR IGNORE INTO dispatch_turns "
                        f"SELECT ?,{column},(SELECT turn FROM dispatch_clock WHERE id=1) "
                        "FROM builds GROUP BY " + column,
                        (scope,),
                    )
                db.execute("CREATE TABLE admission_store ("
                           "id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT NOT NULL,"
                           "state TEXT NOT NULL CHECK(state IN ('pending','ready')))")
                db.execute("INSERT INTO admission_store VALUES(1,?,'pending')", (uuid.uuid4().hex,))
                # Commit the pending marker before copying. Old code rejects version 5;
                # new processes finish migration before accepting any work.
                db.execute("PRAGMA user_version=5")
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        self._initialize_admission()

    def _admission(self, identity: str) -> AdmissionStore:
        return AdmissionStore(self.path.with_name(self.path.name + ".admission"), identity)

    def _initialize_executions(self):
        with self.transaction() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] == 5:
                for statement in EXECUTION_SCHEMA.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute("PRAGMA user_version=6")

    def _initialize_admission(self):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM admission_store WHERE id=1").fetchone()
            if row is None:
                raise IntegrityError("missing admission store reference")
            self.admission = self._admission(row["identity"])
            if row["state"] == "pending":
                self.admission.initialize(db)
                for table in ("quota_policy", "quota_changes", "request_buckets"):
                    db.execute(f"DROP TABLE {table}")
                db.execute("UPDATE admission_store SET state='ready' WHERE id=1")
            else:
                self.admission.enable_wal()
                self.admission.snapshot()
        self._initialize_executions()
        self._initialize_programs()

    def _initialize_programs(self):
        with self.transaction() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] == 6:
                for statement in PROGRAM_SCHEMA.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute("PRAGMA user_version=7")

    def policy(self) -> QuotaPolicy:
        try:
            with self.admission.connection() as db:
                return self.admission.policy(db)
        except (sqlite3.Error, OSError) as error:
            raise IntegrityError("capacity policy is unavailable") from error

    def read_quotas(self, *, shop: str | None = None, identity: str | None = None) -> dict:
        """Local operator API; never exposed through shop grants."""
        capacity = self.admission.snapshot()
        with self.connection() as db:
            db.execute("BEGIN")
            usage = quota_usage(db, at=now(), shop=shop, identity=identity)
        usage["deployment"]["request_buckets"] = capacity.pop("request_buckets")
        return {**capacity, "usage": usage}

    def set_quotas(self, policy: QuotaPolicy, *, expected_generation: int) -> dict:
        try:
            return self.admission.set_policy(policy, expected_generation=expected_generation,
                                             at=now().isoformat())
        except PolicyConflict as error:
            raise Conflict(str(error)) from error

    @contextmanager
    def connection(self, *, timeout=5):
        private_database(self.path)
        db = sqlite3.connect(
            self.path.as_uri() + "?mode=rw", uri=True, timeout=timeout, isolation_level=None
        )
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
        project = SourceProject.model_validate(
            bounded_json(row["project"], max_bytes=MAX_PROJECT_BYTES)
        )
        if project.revision != revision or project.shop_id != shop:
            raise ValueError("stored source integrity check failed")
        return SourceRevision(revision=revision, project=project, created_at=row["created_at"])

    def build_record(self, row) -> BuildRecord:
        try:
            if row is None:
                raise NotFound("build not found")
            # Always hash the bytes read from the current database snapshot.
            # IDs alone cannot detect corruption or an out-of-band row change.
            payload = row["report"]
            if row["state"] in {"succeeded", "failed"} and not payload:
                raise ValueError("terminal build has no report")
            if payload and row["state"] in {"succeeded", "failed"}:
                if not isinstance(payload, bytes) or len(payload) > MAX_OUTPUT_BYTES:
                    raise ValueError("stored build report exceeds its byte/type limits")
                key = (
                    "build",
                    row["id"],
                    row["shop_id"],
                    row["source_revision"],
                    row["state"],
                    row["created_at"],
                    row["finished_at"],
                    row["deadline"],
                    digest(payload),
                )
                return self.data_cache.get(key, lambda: self._decode_build(row))
            return self._decode_build(row)
        except ValueError as error:
            raise IntegrityError(str(error)) from error

    def public_catalog(self, artifact: Artifact) -> dict:
        """Project an already validated artifact, without caching publication state.

        Keep only immutable bytes. Every caller gets fresh containers so edits
        to a response cannot affect later requests or expose another revision.
        """
        payload = self.data_cache.get(
            ("public-catalog", artifact.build_sha256),
            lambda: canonical_json(public_catalog(artifact)),
        )
        return json.loads(payload)

    @staticmethod
    def _decode_build(row) -> BuildRecord:
        if row is None:
            raise NotFound("build not found")
        report = BuildReport.model_validate(bounded_json(row["report"])) if row["report"] else None
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
            return self._claim(db, row, seconds, self.policy())

    def claim_next(self, seconds: float) -> tuple[str, str, SourceProject] | None:
        """Select and claim one eligible job atomically across all broker processes."""
        with self.transaction() as db:
            policy = self.policy()
            running = db.execute("SELECT COUNT(*) FROM builds WHERE state='running'").fetchone()[0]
            if running >= policy.deployment.running_builds:
                return None
            row = db.execute(
                "SELECT b.* FROM builds b "
                "JOIN dispatch_turns s ON s.scope='shop' AND s.subject=b.shop_id "
                "JOIN dispatch_turns a ON a.scope='identity' AND a.subject=b.actor "
                "WHERE b.state='queued' "
                "AND (SELECT COUNT(*) FROM builds r WHERE r.state='running' "
                "AND r.shop_id=b.shop_id)<? "
                "AND (SELECT COUNT(*) FROM builds r WHERE r.state='running' AND r.actor=b.actor)<? "
                "ORDER BY s.turn,a.turn,b.created_at,b.id LIMIT 1",
                (policy.shop.running_builds, policy.identity.running_builds),
            ).fetchone()
            if row is None:
                return None
            claim, source = self._claim(db, row, seconds, policy)
            return row["id"], claim, source

    def _claim(self, db, row, seconds: float, policy: QuotaPolicy) -> tuple[str, SourceProject]:
        record = self.build_record(row)
        if record.state != "queued":
            raise Conflict("build has already been claimed")
        admit_running(db, policy, record.shop_id, row["actor"])
        token = uuid.uuid4().hex
        deadline = now() + timedelta(seconds=seconds)
        db.execute(
            "UPDATE builds SET state='running',claim=?,deadline=? WHERE id=?",
            (token, deadline.isoformat(), record.id),
        )
        db.execute("UPDATE dispatch_clock SET turn=turn+1 WHERE id=1")
        turn = db.execute("SELECT turn FROM dispatch_clock WHERE id=1").fetchone()[0]
        for scope, subject in (("shop", record.shop_id), ("identity", row["actor"])):
            db.execute(
                "UPDATE dispatch_turns SET turn=? WHERE scope=? AND subject=?",
                (turn, scope, subject),
            )
        self.event(db, record.shop_id, "worker", "build.started", record.id)
        return token, self.source(db, record.shop_id, record.source_revision).project

    def finish(self, build_id: str, claim: str, report: BuildReport) -> None:
        report = BuildReport.model_validate(report.model_dump(mode="python"))
        payload = report.model_dump_json().encode()
        if len(payload) > MAX_OUTPUT_BYTES:
            raise ValueError("build report exceeds the storage limit")
        bounded_json(payload)
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
        bounded_json(project.to_json(), max_bytes=MAX_PROJECT_BYTES)
        revision = project.revision
        payload = project.to_json()
        with self.repository.transaction() as db:
            row = db.execute("SELECT head FROM shops WHERE id=?", (project.shop_id,)).fetchone()
            if (row["head"] if row else None) != expected_head:
                raise Conflict("source head changed; read it before saving again")
            if row and row["head"] == revision:
                return self.repository.source(db, project.shop_id, revision)
            existing = db.execute(
                "SELECT 1 FROM sources WHERE shop_id=? AND revision=?",
                (project.shop_id, revision),
            ).fetchone()
            if not existing:
                admit_source(
                    db, self.repository.policy(), project.shop_id, principal.subject, len(payload)
                )
            if row is None:
                db.execute("INSERT INTO shops(id) VALUES(?)", (project.shop_id,))
            db.execute(
                "INSERT OR IGNORE INTO sources VALUES(?,?,?,?,?)",
                (
                    project.shop_id,
                    revision,
                    payload,
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
            if (
                db.execute(
                    "SELECT 1 FROM sources WHERE shop_id=? AND revision=?", (shop, revision)
                ).fetchone()
                is None
            ):
                raise NotFound("source revision not found")
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
            at = now()
            admit_build(db, self.repository.policy(), shop, principal.subject, at)
            # Full source decoding is reserved for an admitted new build. A
            # saturated caller cannot hold the write lock repeatedly parsing it.
            self.repository.source(db, shop, revision)
            db.execute(
                "INSERT INTO builds(id,shop_id,source_revision,state,created_at,actor) "
                "VALUES(?,?,?,'queued',?,?)",
                (build_id, shop, revision, at.isoformat(), principal.subject),
            )
            # New queues join the current turn, never jump ahead with a zero priority.
            turn = db.execute("SELECT turn FROM dispatch_clock WHERE id=1").fetchone()[0]
            for scope, subject in (("shop", shop), ("identity", principal.subject)):
                db.execute(
                    "INSERT OR IGNORE INTO dispatch_turns VALUES(?,?,?)", (scope, subject, turn)
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
        # Publication selection is authoritative on every read, including cache hits.
        with self.repository.connection() as db:
            row = db.execute(
                "SELECT builds.* FROM shops JOIN builds ON shops.published_build=builds.id "
                "WHERE shops.id=? AND builds.shop_id=shops.id",
                (shop,),
            ).fetchone()
            if row is None:
                raise NotFound("published shop not found")
            record = self.repository.build_record(row)
            if record.state != "succeeded" or record.report is None:
                raise ValueError("invalid publication")
            try:
                return self.repository.public_catalog(record.report.artifact)
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
        self._run_claimed(build_id, claim, project)

    def run_next(self) -> bool:
        selected = self.repository.claim_next(self.builder.seconds + 60)
        if selected is None:
            return False
        self._run_claimed(*selected)
        return True

    def _run_claimed(self, build_id: str, claim: str, project: SourceProject) -> None:
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
        # A builder exception can include failed cleanup. Confirm removal even
        # on that path before committing any terminal state or releasing quota.
        self.builder.remove(build_id)
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
