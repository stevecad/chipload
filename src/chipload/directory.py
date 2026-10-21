"""Durable, bounded index of opted-in public supplier listings."""

import base64
import math
import os
import sqlite3
import stat
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import TypeAdapter

from .artifact import canonical_json, digest
from .directory_model import (
    MAX_ADVERTISEMENT_BYTES,
    PublicationReceipt,
    SearchPage,
    SupplierCandidate,
)
from .lifecycle import Conflict, NotFound
from .limits import bounded_json
from .model import Entity, GeoPoint, Id, Listing, SemanticRef
from .storage import enable_wal, private_database


class DirectoryCapacity(RuntimeError):
    pass


def distance(latitude, longitude, other_latitude, other_longitude):
    """Reported straight-line distance on a 6,371 km mean-radius sphere, in m."""
    if latitude is None or other_latitude is None:
        return None
    a, b = math.radians(latitude), math.radians(other_latitude)
    value = math.sin((b - a) / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(math.radians(other_longitude - longitude) / 2) ** 2
    return 12_742_000 * math.asin(math.sqrt(min(1, max(0, value))))


SCHEMA = """
CREATE TABLE metadata(directory_id TEXT NOT NULL, format TEXT NOT NULL);
CREATE TABLE publications(
 instance_id TEXT NOT NULL, shop_id TEXT NOT NULL, owner TEXT NOT NULL,
 version INTEGER NOT NULL, generation INTEGER NOT NULL, revision TEXT NOT NULL,
 origin TEXT, expires REAL, indexed INTEGER NOT NULL, shop_name TEXT NOT NULL,
 last_request TEXT NOT NULL, last_digest TEXT NOT NULL, last_result BLOB NOT NULL,
 PRIMARY KEY(instance_id,shop_id));
CREATE TABLE listings(
 instance_id TEXT NOT NULL, shop_id TEXT NOT NULL, id TEXT NOT NULL,
 profile TEXT NOT NULL, summary BLOB NOT NULL, location BLOB,
 latitude REAL, longitude REAL, accuracy INTEGER,
 PRIMARY KEY(instance_id,shop_id,id),
 FOREIGN KEY(instance_id,shop_id) REFERENCES publications ON DELETE CASCADE);
CREATE INDEX listing_profiles ON listings(profile,instance_id,shop_id,id);
CREATE TABLE fields(
 instance_id TEXT NOT NULL, shop_id TEXT NOT NULL, listing_id TEXT NOT NULL,
 term TEXT NOT NULL, choices INTEGER NOT NULL,
 PRIMARY KEY(instance_id,shop_id,listing_id,term),
 FOREIGN KEY(instance_id,shop_id,listing_id) REFERENCES listings ON DELETE CASCADE);
CREATE TABLE choices(
 instance_id TEXT NOT NULL, shop_id TEXT NOT NULL, listing_id TEXT NOT NULL,
 field TEXT NOT NULL, value TEXT NOT NULL,
 PRIMARY KEY(instance_id,shop_id,listing_id,field,value),
 FOREIGN KEY(instance_id,shop_id,listing_id,field) REFERENCES fields ON DELETE CASCADE);
CREATE TABLE audit(seq INTEGER PRIMARY KEY,instance_id TEXT NOT NULL,actor TEXT NOT NULL,
 action TEXT NOT NULL,shop_id TEXT,version INTEGER,at REAL NOT NULL);
CREATE INDEX audit_instances ON audit(instance_id,seq);
CREATE TABLE epochs(instance_id TEXT PRIMARY KEY,epoch INTEGER NOT NULL);
"""


class DirectoryStore:
    def __init__(self, settings, *, create=False, clock=None):
        self.settings = settings
        self.path = Path(settings.identity.database)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.path.exists() or self.path.is_symlink():
                raise ValueError("initialize only fresh directory storage")
        parent = self.path.parent.stat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise ValueError("directory storage needs an owner-only directory")
        private_database(self.path, create=create)
        with self.connection() as db:
            if create:
                enable_wal(db)
                db.executescript(SCHEMA)
                db.execute("INSERT INTO metadata VALUES(?,?)", (settings.directory_id, "chipload-directory-1"))
                db.commit()
            metadata = db.execute("SELECT directory_id,format FROM metadata").fetchall()
            if len(metadata) != 1 or tuple(metadata[0]) != (settings.directory_id, "chipload-directory-1"):
                raise ValueError("directory storage identity or format does not match")

    @contextmanager
    def connection(self):
        private_database(self.path)
        db = sqlite3.connect(self.path, timeout=1)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.create_function("distance", 4, distance, deterministic=True)
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
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def audit(self, db, instance, actor, action, shop=None, version=None):
        db.execute("INSERT INTO audit(instance_id,actor,action,shop_id,version,at) VALUES(?,?,?,?,?,?)",
                   (instance, actor, action, shop, version, self.clock().timestamp()))
        db.execute("DELETE FROM audit WHERE instance_id=? AND seq NOT IN "
                   "(SELECT seq FROM audit WHERE instance_id=? ORDER BY seq DESC LIMIT 256)", (instance, instance))

    def next_epoch(self, instance, actor):
        with self.transaction() as db:
            row = db.execute("SELECT epoch FROM epochs WHERE instance_id=?", (instance,)).fetchone()
            epoch = row[0] + 1 if row else 1
            if row is None and db.execute("SELECT count(*) FROM epochs").fetchone()[0] >= 4096:
                raise DirectoryCapacity("session identity capacity exhausted")
            if epoch >= 2**63 - 1:
                raise DirectoryCapacity("session generation exhausted")
            db.execute("INSERT INTO epochs VALUES(?,?) ON CONFLICT(instance_id) DO UPDATE SET epoch=excluded.epoch", (instance, epoch))
            self.audit(db, instance, actor, "session", version=epoch)
            return epoch

    def head(self, instance, shop):
        with self.connection() as db:
            row = db.execute("SELECT last_result FROM publications WHERE instance_id=? AND shop_id=?", (instance, shop)).fetchone()
            if row is None:
                raise NotFound("advertisement not found")
            return PublicationReceipt.model_validate(bounded_json(bytes(row[0])))

    @staticmethod
    def projection(catalog):
        value = catalog.value
        summary = Entity.model_validate({**value["shop"], "visibility": "public"})
        sites = {TypeAdapter(Id).validate_python(site["id"]): site for site in value["sites"]}
        if len(sites) != len(value["sites"]):
            raise ValueError("duplicate public site IDs")
        targets = {kind: {item["id"] for item in value.get(collection, ())}
                   for kind, collection in (("capability", "capabilities"), ("workflow", "workflows"), ("offer", "offers"))}
        entries, identifiers = [], set()
        for data in value.get("listings", ()):
            if "visibility" in data:
                raise ValueError("public listing projections omit visibility")
            listing = Listing.model_validate({**data, "visibility": "public"})
            if listing.id in identifiers:
                raise ValueError("duplicate public listing IDs")
            identifiers.add(listing.id)
            if not listing.indexed:
                continue
            if listing.target not in targets[listing.target_kind] or (listing.site is not None and listing.site not in sites):
                raise ValueError("listing target and site must be public")
            location = sites[listing.site].get("public_location") if listing.site is not None else None
            location = GeoPoint.model_validate(location) if location is not None else None
            brief = listing.model_dump(mode="json", include={"id", "name", "description", "profile", "site", "target_kind", "target"})
            entries.append((listing, brief, location))
        return summary.name, entries

    def advertise(self, instance, shop, actor, advertisement):
        encoded = canonical_json(advertisement)
        if len(encoded) > MAX_ADVERTISEMENT_BYTES:
            raise ValueError("advertisement exceeds its byte limit")
        if advertisement.catalog.value["shop"]["id"] != shop:
            raise ValueError("advertisement and path shop IDs must match")
        if advertisement.catalog.generation >= 2**63 - 1:
            raise ValueError("source generation exceeds its limit")
        body_hash = digest(encoded)
        name, entries = self.projection(advertisement.catalog)
        with self.transaction() as db:
            previous = db.execute("SELECT * FROM publications WHERE instance_id=? AND shop_id=?", (instance, shop)).fetchone()
            repeated = self.repeat(previous, actor, advertisement.request_id, body_hash)
            if repeated:
                return repeated
            version = self.check_version(previous, advertisement.expected_version)
            now = self.clock()
            age = (now - advertisement.observed_at).total_seconds()
            if not -5 <= age <= 30:
                raise ValueError("advertisement requires a source observation within 30 seconds")
            if previous and (advertisement.catalog.generation < previous["generation"] or (
                advertisement.catalog.generation == previous["generation"] and advertisement.catalog.revision != previous["revision"]
            )):
                raise Conflict("source publication generation cannot move backwards or change meaning")
            expires = min(now, advertisement.observed_at) + timedelta(seconds=advertisement.lease_seconds)
            db.execute("DELETE FROM listings WHERE EXISTS(SELECT 1 FROM publications h WHERE "
                       "h.instance_id=listings.instance_id AND h.shop_id=listings.shop_id AND h.expires<=?)", (now.timestamp(),))
            db.execute("DELETE FROM listings WHERE instance_id=? AND shop_id=?", (instance, shop))
            receipt = PublicationReceipt(instance_id=instance, shop_id=shop, version=version,
                request_id=advertisement.request_id, source_generation=advertisement.catalog.generation,
                source_revision=advertisement.catalog.revision, expires_at=expires, indexed_listings=len(entries))
            if not previous:
                total = db.execute("SELECT count(*) FROM publications").fetchone()[0]
                owned = db.execute("SELECT count(*) FROM publications WHERE instance_id=?", (instance,)).fetchone()[0]
                if total >= self.settings.max_publications or owned >= 64:
                    raise DirectoryCapacity("directory publication capacity exhausted")
            self.write_head(db, receipt, actor, advertisement.origin, name, body_hash)
            additions = (len(entries), sum(len(item.fields) for item, _, _ in entries),
                         sum(len(field.choices) for item, _, _ in entries for field in item.fields))
            for (table, maximum, owned_max), addition in zip((
                ("listings", self.settings.max_listings, 512),
                ("fields", self.settings.max_fields, 4096),
                ("choices", self.settings.max_choices, 8192),
            ), additions, strict=True):
                counts = db.execute(f"SELECT count(*),sum(instance_id=?) FROM {table}", (instance,)).fetchone()
                if counts[0] + addition > maximum or (counts[1] or 0) + addition > owned_max:
                    raise DirectoryCapacity("directory index capacity exhausted")
            for listing, brief, location in entries:
                db.execute("INSERT INTO listings VALUES(?,?,?,?,?,?,?,?,?)", (
                    instance, shop, listing.id, canonical_json(listing.profile).decode(), canonical_json(brief),
                    canonical_json(location) if location else None,
                    float(location.latitude) if location else None, float(location.longitude) if location else None,
                    location.accuracy_m if location else None,
                ))
                for field in listing.fields:
                    key = canonical_json(field.term).decode()
                    db.execute("INSERT INTO fields VALUES(?,?,?,?,?)", (instance, shop, listing.id, key, len(field.choices)))
                    db.executemany("INSERT INTO choices VALUES(?,?,?,?,?)", [
                        (instance, shop, listing.id, key, canonical_json(choice.term).decode()) for choice in field.choices
                    ])
            self.audit(db, instance, actor, "advertise", shop, version)
            return receipt

    @staticmethod
    def repeat(previous, actor, request_id, body_hash):
        if previous and previous["owner"] != actor:
            raise Conflict("advertisement belongs to another enrolled owner")
        if previous and previous["last_request"] == request_id:
            if previous["last_digest"] != body_hash:
                raise Conflict("request ID was already used with different input")
            return PublicationReceipt.model_validate(bounded_json(bytes(previous["last_result"])))
        return None

    @staticmethod
    def check_version(previous, expected):
        version = previous["version"] if previous else 0
        if version != expected:
            raise Conflict("advertisement changed; read its current version")
        if version >= 2**63 - 2:
            raise DirectoryCapacity("advertisement generation exhausted")
        return version + 1

    @staticmethod
    def write_head(db, receipt, actor, origin, name, body_hash):
        db.execute("INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(instance_id,shop_id) DO UPDATE SET "
                   "owner=excluded.owner,version=excluded.version,generation=excluded.generation,revision=excluded.revision,"
                   "origin=excluded.origin,expires=excluded.expires,indexed=excluded.indexed,shop_name=excluded.shop_name,"
                   "last_request=excluded.last_request,last_digest=excluded.last_digest,last_result=excluded.last_result", (
            receipt.instance_id, receipt.shop_id, actor, receipt.version, receipt.source_generation,
            receipt.source_revision, origin, receipt.expires_at.timestamp() if receipt.expires_at else None,
            receipt.indexed_listings, name, receipt.request_id, body_hash, canonical_json(receipt),
        ))

    def withdraw(self, instance, shop, actor, withdrawal):
        with self.transaction() as db:
            previous = db.execute("SELECT * FROM publications WHERE instance_id=? AND shop_id=?", (instance, shop)).fetchone()
            if not previous:
                raise NotFound("advertisement not found")
            body_hash = digest(canonical_json(withdrawal))
            repeated = self.repeat(previous, actor, withdrawal.request_id, body_hash)
            if repeated:
                return repeated
            version = self.check_version(previous, withdrawal.expected_version)
            receipt = PublicationReceipt(instance_id=instance, shop_id=shop, version=version,
                request_id=withdrawal.request_id, source_generation=previous["generation"],
                source_revision=previous["revision"], expires_at=None, indexed_listings=0)
            db.execute("DELETE FROM listings WHERE instance_id=? AND shop_id=?", (instance, shop))
            self.write_head(db, receipt, actor, None, previous["shop_name"], body_hash)
            self.audit(db, instance, actor, "withdraw", shop, version)
            return receipt

    def search(self, query, *, connected=frozenset()):
        now = self.clock()
        criteria = query.model_dump(mode="json", exclude={"cursor"})
        query_hash = digest(canonical_json(criteria))
        allowed, owners = [], []
        for binding in self.settings.bindings:
            if binding.advertise:
                allowed.append("(?,?)")
                owners.extend((binding.instance_id, binding.subject))
        ownership = ("(h.instance_id,h.owner) IN (VALUES " + ",".join(allowed) + ")") if allowed else "0"
        with self.connection() as db:
            db.execute("BEGIN")
            heads = db.execute("SELECT h.instance_id,h.shop_id,h.version,h.expires FROM publications h "
                "WHERE h.expires>? AND " + ownership + " AND EXISTS(SELECT 1 FROM listings l WHERE l.instance_id=h.instance_id AND l.shop_id=h.shop_id) "
                "ORDER BY h.instance_id,h.shop_id", [now.timestamp(), *owners]).fetchall()
            snapshot = digest(canonical_json({"directory": self.settings.directory_id, "heads": [tuple(row) for row in heads]}))
            after = (-1, "", "", "")
            if query.cursor:
                try:
                    cursor = bounded_json(base64.b64decode(query.cursor, altchars=b"-_", validate=True), max_bytes=2048)
                    if set(cursor) != {"snapshot", "query", "after"} or cursor["query"] != query_hash:
                        raise ValueError()
                    if cursor["snapshot"] != snapshot:
                        raise Conflict("directory snapshot changed; restart search")
                    after = cursor["after"]
                    if (not isinstance(after, list) or len(after) != 4 or type(after[0]) not in (int, float)
                            or not math.isfinite(after[0]) or not 0 <= after[0] <= 21_000_000):
                        raise ValueError()
                    TypeAdapter(Id).validate_python(after[2])
                    TypeAdapter(Id).validate_python(after[3])
                    from .peer_model import InstanceId
                    TypeAdapter(InstanceId).validate_python(after[1])
                except Conflict:
                    raise
                except (ValueError, TypeError, KeyError):
                    raise ValueError("invalid directory cursor") from None
            conditions = ["h.expires>?", ownership, "l.profile=?"]
            parameters = [now.timestamp(), *owners, canonical_json(query.profile).decode()]
            for item in query.inputs:
                key = canonical_json(item.term).decode()
                match = "f.instance_id=l.instance_id AND f.shop_id=l.shop_id AND f.listing_id=l.id AND f.term=?"
                if isinstance(item.value, SemanticRef):
                    conditions.append("EXISTS(SELECT 1 FROM fields f JOIN choices c ON c.instance_id=f.instance_id AND c.shop_id=f.shop_id AND c.listing_id=f.listing_id AND c.field=f.term WHERE " + match + " AND c.value=?)")
                    parameters.extend((key, canonical_json(item.value).decode()))
                else:
                    conditions.append("EXISTS(SELECT 1 FROM fields f WHERE " + match + " AND f.choices=0)")
                    parameters.append(key)
            latitude = float(query.origin.latitude) if query.origin else None
            longitude = float(query.origin.longitude) if query.origin else None
            if query.origin:
                conditions.append("(l.latitude IS NULL OR l.accuracy<=?)" if query.include_unlocated else "(l.latitude IS NOT NULL AND l.accuracy<=?)")
                parameters.append(query.max_accuracy_m)
                if query.radius_m is not None:
                    conditions.append("(l.latitude IS NULL OR distance(?,?,l.latitude,l.longitude)<=?)")
                    parameters.extend((latitude, longitude, query.radius_m))
            sql = "WITH matches AS (SELECT l.*,h.generation,h.revision,h.expires,h.origin,h.shop_name," \
                  "distance(?,?,l.latitude,l.longitude) AS meters,coalesce(distance(?,?,l.latitude,l.longitude),21000000) AS rank " \
                  "FROM listings l JOIN publications h ON h.instance_id=l.instance_id AND h.shop_id=l.shop_id WHERE " \
                  + " AND ".join(conditions) + ") SELECT * FROM matches WHERE (rank,instance_id,shop_id,id)>(?,?,?,?) " \
                  "ORDER BY rank,instance_id,shop_id,id LIMIT ?"
            rows = db.execute(sql, [latitude, longitude, latitude, longitude, *parameters, *after, query.limit + 1]).fetchall()
        following = None
        if len(rows) > query.limit:
            last = rows[query.limit - 1]
            following = base64.urlsafe_b64encode(canonical_json({"snapshot": snapshot, "query": query_hash,
                "after": [last["rank"], last["instance_id"], last["shop_id"], last["id"]]})).decode()
        candidates = []
        for row in rows[:query.limit]:
            location = GeoPoint.model_validate(bounded_json(bytes(row["location"]))) if row["location"] else None
            candidates.append(SupplierCandidate(instance_id=row["instance_id"], shop_id=row["shop_id"],
                shop_name=row["shop_name"], listing=bounded_json(bytes(row["summary"])),
                source_generation=row["generation"], source_revision=row["revision"],
                expires_at=datetime.fromtimestamp(row["expires"], timezone.utc), direct_origin=row["origin"],
                relay_available=row["instance_id"] in connected, location=location,
                distance_m=round(row["meters"], 3) if row["meters"] is not None else None,
                distance_uncertainty_m=location.accuracy_m + query.origin.accuracy_m if location and query.origin else None))
        return SearchPage(directory_id=self.settings.directory_id, snapshot=snapshot, query_sha256=query_hash,
                          evaluated_at=now, candidates=tuple(candidates), next_cursor=following)
