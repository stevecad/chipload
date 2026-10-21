"""Queue ownership, atomic claims, bounded capacity and historical network receipts."""

import sqlite3
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from chipload.access import Forbidden, Grant, Principal
from chipload.artifact import canonical_json
from chipload.directory_model import SearchPage, SupplierSearch
from chipload.lifecycle import Conflict, NotFound, Repository, ShopService
from chipload.network import NetworkService
from chipload.network_model import NetworkComplete, NetworkSubmit, search_hash
from chipload.quotas import QuotaExceeded
from examples.federation_shops import project, term


class NetworkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "chipload.db"
        self.repository = Repository(self.path)
        self.now = datetime.now(timezone.utc)
        self.service = NetworkService(self.repository, clock=lambda: self.now)
        self.owner = Principal(subject="buyer", grants=(Grant(shop_id="mill", actions=("network:query", "source:write")),))
        self.other = self.owner.model_copy(update={"subject": "other"})
        self.dispatcher = Principal(subject="connector", grants=(Grant(shop_id="mill", actions=("network:dispatch",)),))
        ShopService(self.repository).save_source(self.owner, project("mill"), expected_head=None)

    def intent(self):
        return NetworkSubmit(shop_id="mill", request_key=uuid.uuid4().hex, deadline=self.now + timedelta(seconds=30),
            query={"kind": "search", "cloud": "suppliers", "directory_id": "c" * 32,
                   "query": SupplierSearch(profile=term("material-supply"))})

    def result(self, intent, **changes):
        return SearchPage(directory_id="c" * 32, snapshot="d" * 64,
            query_sha256=search_hash(intent.query.query), evaluated_at=self.now,
            candidates=(), next_cursor=None).model_copy(update=changes)

    def test_exact_retry_ownership_and_scoped_dispatch(self):
        intent = self.intent()
        record = self.service.submit(self.owner, intent)
        self.assertEqual(self.service.submit(self.owner, intent), record)
        with self.assertRaises(Conflict):
            self.service.submit(self.owner, intent.model_copy(update={"deadline": self.now + timedelta(seconds=20)}))
        with self.assertRaises(NotFound):
            self.service.read(self.other, "mill", record.id)
        with self.assertRaises(Forbidden):
            self.service.claim(self.owner, "mill")
        lease = self.service.claim(self.dispatcher, "mill")["lease"]
        self.assertEqual(lease["job_id"], record.id)
        with self.assertRaises(Forbidden):
            self.service.complete(self.owner, NetworkComplete(shop_id="mill", job_id=record.id, claim=lease["claim"], result=self.result(intent)))
        self.service.complete(self.dispatcher, NetworkComplete(shop_id="mill", job_id=record.id, claim=lease["claim"], result=self.result(intent)))
        self.assertEqual(self.service.read(self.owner, "mill", record.id).state, "succeeded")
        with self.assertRaises((Conflict, NotFound)):
            self.service.complete(self.dispatcher, NetworkComplete(shop_id="mill", job_id=record.id, claim=lease["claim"], result=self.result(intent)))

    def test_cancellation_and_deadline_fence_late_or_replaced_replies(self):
        intent = self.intent()
        record = self.service.submit(self.owner, intent)
        lease = self.service.claim(self.dispatcher, "mill")["lease"]
        self.service.cancel(self.owner, "mill", record.id)
        with self.assertRaises((Conflict, NotFound)):
            self.service.complete(self.dispatcher, NetworkComplete(shop_id="mill", job_id=record.id, claim=lease["claim"], result=self.result(intent)))
        intent = self.intent()
        record = self.service.submit(self.owner, intent)
        lease = self.service.claim(self.dispatcher, "mill")["lease"]
        self.now += timedelta(seconds=31)
        self.assertEqual(self.service.read(self.owner, "mill", record.id).state, "expired")
        with self.assertRaises((Conflict, NotFound)):
            self.service.complete(self.dispatcher, NetworkComplete(shop_id="mill", job_id=record.id, claim=lease["claim"], result=self.result(intent)))
        self.assertIsNone(self.service.claim(self.dispatcher, "mill")["lease"])

    def test_result_identity_and_query_must_match_before_settlement(self):
        intent = self.intent()
        record = self.service.submit(self.owner, intent)
        lease = self.service.claim(self.dispatcher, "mill")["lease"]
        for changes in ({"directory_id": "a" * 32}, {"query_sha256": "a" * 64},
                        {"evaluated_at": self.now - timedelta(seconds=31)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.service.complete(self.dispatcher, NetworkComplete(shop_id="mill", job_id=record.id,
                    claim=lease["claim"], result=self.result(intent, **changes)))
        self.assertEqual(self.service.read(self.owner, "mill", record.id).state, "running")

    def test_concurrent_claims_are_unique_and_per_caller_capacity_is_bounded(self):
        for _ in range(8):
            self.service.submit(self.owner, self.intent())
        with self.assertRaises(QuotaExceeded):
            self.service.submit(self.owner, self.intent())
        with ThreadPoolExecutor(max_workers=4) as workers:
            leases = list(workers.map(lambda _: self.service.claim(self.dispatcher, "mill")["lease"], range(4)))
        actual = [lease for lease in leases if lease is not None]
        self.assertEqual(len(actual), 2)
        self.assertEqual(len({lease["job_id"] for lease in actual}), 2)
        reopened = NetworkService(Repository(self.path), clock=lambda: self.now)
        self.assertIsNone(reopened.claim(self.dispatcher, "mill")["lease"])
        self.now += timedelta(seconds=31)
        self.assertIsNone(reopened.claim(self.dispatcher, "mill")["lease"])
        self.assertEqual(self.service.read(self.owner, "mill", actual[0]["job_id"]).state, "expired")

    def test_expired_intents_cannot_be_dispatched_after_history_pruning(self):
        intent = self.intent()
        record = self.service.submit(self.owner, intent)
        self.service.cancel(self.owner, "mill", record.id)
        self.now += timedelta(hours=2)
        self.service.submit(self.owner, self.intent())
        with self.assertRaises(ValueError):
            self.service.submit(self.owner, intent)
        with self.repository.connection() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM network_jobs WHERE id=?", (record.id,)).fetchone())
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 12)
        self.assertLess(len(canonical_json(intent)), 128 * 1024)

    def test_schema_eleven_migration_is_atomic_and_concurrent(self):
        from chipload import lifecycle

        identity = self.repository.admission.identity
        with self.repository.transaction() as db:
            db.execute("DROP TABLE network_jobs")
            db.execute("PRAGMA user_version=11")
        with patch.object(lifecycle, "NETWORK_SCHEMA", lifecycle.NETWORK_SCHEMA + "INVALID SQL;"):
            with self.assertRaises(sqlite3.OperationalError):
                Repository(self.path)
        with self.repository.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 11)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='network_jobs'").fetchone())
        with ThreadPoolExecutor(max_workers=4) as workers:
            opened = list(workers.map(lambda _: Repository(self.path), range(8)))
        self.assertTrue(all(repo.admission.identity == identity for repo in opened))
        self.assertEqual(self.service.submit(self.owner, self.intent()).state, "queued")

    def test_paired_recovery_preserves_receipts_and_never_replays_expired_reads(self):
        from recovery_support import archive

        record = self.service.submit(self.owner, self.intent())
        self.assertIsNotNone(self.service.claim(self.dispatcher, "mill")["lease"])
        destination = self.path.parent / "snapshot"
        archive.snapshot_pair(self.path.parent, destination)
        restored = NetworkService(Repository(destination / "chipload.db"), clock=lambda: self.now)
        self.assertEqual(restored.read(self.owner, "mill", record.id).state, "running")
        self.now += timedelta(seconds=31)
        self.assertIsNone(restored.claim(self.dispatcher, "mill")["lease"])
        self.assertEqual(restored.read(self.owner, "mill", record.id).state, "expired")

    def test_a_claimed_result_can_finish_after_dispatch_admission_credit_is_spent(self):
        from chipload.operations import Operations
        from chipload.quotas import QuotaPolicy

        state = self.repository.read_quotas()
        state["policy"]["requests"]["mutation_identity"] = {"burst": 1, "per_minute": 1}
        self.repository.set_quotas(QuotaPolicy.model_validate(state["policy"]), expected_generation=state["generation"])
        operations = Operations(ShopService(self.repository))
        operations.network = self.service
        intent = self.intent()
        record = operations.call("network_submit", self.owner, intent)
        lease = operations.call("network_claim", self.dispatcher, {"shop_id": "mill"})["lease"]
        self.assertEqual(operations.call("network_complete", self.dispatcher, NetworkComplete(
            shop_id="mill", job_id=record["id"], claim=lease["claim"], result=self.result(intent))), {"accepted": True})
