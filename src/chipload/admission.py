"""Durable capacity policy and request accounting, independent of shop write locks."""

import sqlite3
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from .artifact import canonical_json
from .limits import bounded_json
from .quotas import QuotaPolicy
from .request_limits import REQUEST_SCHEMA, update_request_rates
from .storage import private_database


class PolicyConflict(ValueError):
    pass


@lru_cache(maxsize=8)
def decoded_policy(payload: bytes) -> QuotaPolicy:
    # Cache only validation of identical bounded bytes. Every transaction still
    # reads its current policy row; generations and balances are never cached.
    return QuotaPolicy.model_validate(bounded_json(payload, max_bytes=16 * 1024))


SCHEMA = (
    """
CREATE TABLE admission_identity (id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT NOT NULL);
CREATE TABLE quota_policy (
    id INTEGER PRIMARY KEY CHECK(id=1), generation INTEGER NOT NULL, policy BLOB NOT NULL
);
CREATE TABLE quota_changes (
    generation INTEGER PRIMARY KEY, policy BLOB NOT NULL, time TEXT NOT NULL
);
"""
    + REQUEST_SCHEMA
)


class AdmissionStore:
    def __init__(self, path: Path, identity: str):
        self.path, self.identity = path, identity

    def _private_file(self, *, create=False):
        try:
            private_database(self.path, create=create)
        except ValueError as error:
            raise sqlite3.DatabaseError(str(error)) from error

    @contextmanager
    def _connection(self, *, timeout=5):
        self._private_file()
        db = sqlite3.connect(
            self.path.as_uri() + "?mode=rw", uri=True, timeout=timeout, isolation_level=None
        )
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            db.close()

    def _check_identity(self, db):
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise sqlite3.DatabaseError("unsupported admission database schema")
        row = db.execute("SELECT identity FROM admission_identity WHERE id=1").fetchone()
        if row is None or row["identity"] != self.identity:
            raise sqlite3.DatabaseError("admission database does not match the shop repository")

    @contextmanager
    def connection(self, *, timeout=5):
        with self._connection(timeout=timeout) as db:
            self._check_identity(db)
            yield db

    def enable_wal(self):
        """Restore runtime journaling after an offline SQLite backup is installed."""
        with self._connection() as db:
            self._check_identity(db)
            if db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise sqlite3.DatabaseError("admission database requires WAL journaling")

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

    def initialize(self, source):
        """Copy frozen legacy state. The repository must hold its migration write lock.

        This may repeat after an interrupted migration. No caller can use the new
        store until the repository commits its ready marker.
        """
        self._private_file(create=True)
        with self._connection() as db:
            objects = db.execute(
                "SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if objects:
                self._check_identity(db)
            elif db.execute("PRAGMA user_version").fetchone()[0] != 0:
                raise sqlite3.DatabaseError("unrecognized admission database")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            try:
                if not objects:
                    for statement in SCHEMA.split(";"):
                        if statement.strip():
                            db.execute(statement)
                    db.execute("INSERT INTO admission_identity VALUES(1,?)", (self.identity,))
                    db.execute("PRAGMA user_version=1")
                self.policy(source)  # Validate the policy before copying any state.
                for table in ("quota_policy", "quota_changes", "request_buckets"):
                    db.execute(f"DELETE FROM {table}")
                    cursor = source.execute(f"SELECT * FROM {table}")
                    placeholders = ",".join("?" for _ in cursor.description)
                    db.executemany(f"INSERT INTO {table} VALUES({placeholders})", cursor)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    @staticmethod
    def policy(db) -> QuotaPolicy:
        row = db.execute("SELECT policy FROM quota_policy WHERE id=1").fetchone()
        if row is None:
            raise sqlite3.DatabaseError("missing capacity policy")
        try:
            return decoded_policy(row["policy"])
        except ValueError as error:
            raise sqlite3.DatabaseError("invalid capacity policy") from error

    def snapshot(self):
        with self.connection() as db:
            db.execute("BEGIN")
            policy = self.policy(db)
            return {
                "generation": db.execute(
                    "SELECT generation FROM quota_policy WHERE id=1"
                ).fetchone()[0],
                "policy": policy.model_dump(mode="json"),
                "request_buckets": db.execute("SELECT COUNT(*) FROM request_buckets").fetchone()[0],
            }

    def set_policy(self, policy: QuotaPolicy, *, expected_generation: int, at: str):
        policy = QuotaPolicy.model_validate(policy.model_dump(mode="python"))
        payload = canonical_json(policy)
        with self.transaction() as db:
            previous = self.policy(db)
            generation = db.execute("SELECT generation FROM quota_policy WHERE id=1").fetchone()[0]
            if generation != expected_generation:
                raise PolicyConflict("quota policy changed; read its generation before updating")
            update_request_rates(db, previous.requests, policy.requests)
            db.execute(
                "UPDATE quota_policy SET generation=?,policy=? WHERE id=1",
                (generation + 1, payload),
            )
            db.execute("INSERT INTO quota_changes VALUES(?,?,?)", (generation + 1, payload, at))
            return {"generation": generation + 1, "policy": policy.model_dump(mode="json")}
