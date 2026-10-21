"""Admission and dispatch under competing identities, shops, and processes."""

import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from chipload.access import ACTIONS, Forbidden, Grant, Principal
from chipload.lifecycle import SCHEMA, Conflict, Repository, ShopService
from chipload.project import MAX_OUTPUT_BYTES, BuildReport, SourceFile, SourceProject
from chipload.quotas import QuotaExceeded, QuotaPolicy
from chipload.validation import Diagnostic

AT = datetime(2026, 10, 20, tzinfo=timezone.utc)


def enqueue_process(arguments):
    path, shop, revision, actor, key = arguments
    service = ShopService(Repository(Path(path)))
    who = Principal(subject=actor, grants=(Grant(shop_id=shop, actions=ACTIONS),))
    try:
        return service.request_build(who, shop, revision, request_key=key).id
    except QuotaExceeded:
        return "limited"


def claim_process(path):
    result = Repository(Path(path)).claim_next(60)
    return result[0] if result else None


class QuotaTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "quota.db"
        self.repository = Repository(self.path)
        self.service = ShopService(self.repository)

    def who(self, actor, shop):
        return Principal(subject=actor, grants=(Grant(shop_id=shop, actions=ACTIONS),))

    def source(self, shop="shop-a", actor="owner"):
        project = SourceProject(
            shop_id=shop,
            entrypoint="shop:define",
            files=(SourceFile(path="shop.py", content="def define(): pass\n"),),
        )
        return self.service.save_source(self.who(actor, shop), project, expected_head=None)

    def policy(self, **changes):
        current = self.repository.read_quotas()
        payload = current["policy"]
        for scope, values in changes.items():
            payload[scope].update(values)
        return self.repository.set_quotas(
            QuotaPolicy.model_validate(payload), expected_generation=current["generation"]
        )

    def enqueue(self, shop, revision, actor="owner", key=None):
        return self.service.request_build(self.who(actor, shop), shop, revision, request_key=key)

    def finish_failed(self, selected):
        build, claim, source = selected
        self.repository.finish(
            build,
            claim,
            BuildReport(
                source_revision=source.revision,
                worker_image="sha256:" + "a" * 64,
                status="failed",
                diagnostics=(Diagnostic(code="test-failure", path=(), message="Test"),),
            ),
        )

    def test_policy_is_shared_and_updates_have_a_generation_guard(self):
        other = Repository(self.path)
        self.policy(shop={"pending_builds": 2})
        self.assertEqual(other.read_quotas()["policy"]["shop"]["pending_builds"], 2)
        with self.assertRaises(Conflict):
            other.set_quotas(QuotaPolicy(), expected_generation=0)
        self.assertEqual(Repository(self.path).read_quotas(), self.repository.read_quotas())

    def test_usage_reports_stored_sources_and_reserved_reports_in_one_snapshot(self):
        source = self.source()
        self.enqueue("shop-a", source.revision)
        status = self.repository.read_quotas(shop="shop-a", identity="owner")
        for scope in ("deployment", "shop", "identity"):
            usage = status["usage"][scope]
            self.assertEqual(usage["retained_sources"], 1)
            self.assertEqual(usage["source_bytes"], len(source.project.to_json()))
            self.assertEqual(usage["pending_builds"], 1)
            self.assertEqual(usage["running_builds"], 0)
            self.assertEqual(usage["report_bytes_reserved_or_stored"], MAX_OUTPUT_BYTES)

    def test_local_operator_can_inspect_usage_and_update_with_a_guard(self):
        source = self.source()
        self.enqueue("shop-a", source.revision)
        command = [sys.executable, "-m", "chipload.local", "--database", str(self.path)]
        status = subprocess.run(
            [*command, "quotas", "--shop", "shop-a", "--identity", "owner"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        data = json.loads(status.stdout)
        self.assertEqual(data["usage"]["identity"]["pending_builds"], 1)
        data["policy"]["shop"]["pending_builds"] = 2
        config = self.path.parent / "quota-policy.json"
        config.write_text(json.dumps(data["policy"]))
        change = [
            *command,
            "set-quotas",
            str(config),
            "--expected-generation",
            str(data["generation"]),
        ]
        result = subprocess.run(change, capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual(json.loads(result.stdout)["generation"], 1)
        stale = subprocess.run(change, capture_output=True, text=True, timeout=10)
        self.assertEqual(stale.returncode, 1)
        self.assertIn("quota policy changed", stale.stderr)
        self.assertEqual(self.repository.read_quotas()["policy"]["shop"]["pending_builds"], 2)

    def test_source_storage_limit_preserves_head_and_accepts_noop(self):
        first = self.source()
        self.policy(shop={"retained_sources": 1, "source_bytes": len(first.project.to_json())})
        owner = self.who("owner", "shop-a")
        unchanged = self.service.save_source(owner, first.project, expected_head=first.revision)
        self.assertEqual(unchanged.revision, first.revision)
        changed = first.project.model_copy(
            update={
                "files": (
                    first.project.files[0].model_copy(
                        update={"content": "def define(): return None\n"}
                    ),
                )
            }
        )
        with self.assertRaises(QuotaExceeded):
            self.service.save_source(owner, changed, expected_head=first.revision)
        self.assertEqual(self.service.get_source(owner, "shop-a").revision, first.revision)
        self.policy(shop={"retained_sources": 2})
        with self.assertRaisesRegex(QuotaExceeded, "source_bytes"):
            self.service.save_source(owner, changed, expected_head=first.revision)

    def test_identity_source_budget_applies_across_shops(self):
        self.source()
        self.policy(identity={"retained_sources": 1})
        with self.assertRaisesRegex(QuotaExceeded, "identity.retained_sources"):
            self.source("shop-b")
        # A denial must not leave a new empty shop behind.
        with self.repository.connection() as db:
            self.assertIsNone(db.execute("SELECT id FROM shops WHERE id='shop-b'").fetchone())
        self.source("shop-b", "another")

    def test_retry_key_is_returned_before_full_quota_and_is_bound_to_source(self):
        source = self.source()
        self.policy(shop={"pending_builds": 1})
        first = self.enqueue("shop-a", source.revision, key="same")
        with patch.object(
            self.repository, "source", side_effect=AssertionError("unnecessary decode")
        ):
            self.assertEqual(self.enqueue("shop-a", source.revision, key="same").id, first.id)
            with self.assertRaisesRegex(QuotaExceeded, "pending_builds"):
                self.enqueue("shop-a", source.revision, key="new")
        changed = source.project.model_copy(
            update={
                "files": (
                    source.project.files[0].model_copy(update={"content": "# another revision\n"}),
                )
            }
        )
        saved = self.service.save_source(
            self.who("owner", "shop-a"), changed, expected_head=source.revision
        )
        with self.assertRaisesRegex(Conflict, "different source"):
            self.enqueue("shop-a", saved.revision, key="same")

    def test_pending_and_hourly_limits_cannot_be_evaded_across_shops(self):
        one, two = self.source(), self.source("shop-b")
        self.policy(identity={"pending_builds": 1, "builds_per_hour": 1})
        with patch("chipload.lifecycle.now", return_value=AT):
            self.enqueue("shop-a", one.revision)
            with self.assertRaisesRegex(QuotaExceeded, "identity.pending_builds"):
                self.enqueue("shop-b", two.revision)
            self.finish_failed(self.repository.claim_next(60))
            with self.assertRaises(QuotaExceeded) as failure:
                self.enqueue("shop-b", two.revision)
        self.assertEqual(failure.exception.quota, "identity.builds_per_hour")
        self.assertEqual(failure.exception.retry_after, 3600)
        with patch("chipload.lifecycle.now", return_value=AT + timedelta(hours=1)):
            self.enqueue("shop-b", two.revision)

    def test_reservations_cover_report_storage_and_release_unused_bytes(self):
        source = self.source()
        self.policy(shop={"report_bytes": MAX_OUTPUT_BYTES})
        self.enqueue("shop-a", source.revision)
        with self.assertRaisesRegex(QuotaExceeded, "report_bytes"):
            self.enqueue("shop-a", source.revision)
        self.finish_failed(self.repository.claim_next(60))
        with self.repository.connection() as db:
            size = db.execute("SELECT length(report) FROM builds").fetchone()[0]
        # Completed bytes remain charged; only the unused reservation is released.
        with self.assertRaisesRegex(QuotaExceeded, "report_bytes"):
            self.enqueue("shop-a", source.revision)
        self.policy(shop={"report_bytes": MAX_OUTPUT_BYTES + size})
        self.enqueue("shop-a", source.revision)

    def test_retained_build_limit_counts_completed_jobs(self):
        source = self.source()
        self.enqueue("shop-a", source.revision)
        self.finish_failed(self.repository.claim_next(60))
        self.policy(shop={"retained_builds": 1})
        with self.assertRaisesRegex(QuotaExceeded, "retained_builds"):
            self.enqueue("shop-a", source.revision)

    def test_source_admission_race_has_no_partial_shops_or_revisions(self):
        self.policy(deployment={"retained_sources": 1})

        def save(number):
            service = ShopService(Repository(self.path))
            shop = f"shop-{number}"
            source = SourceProject(
                shop_id=shop,
                entrypoint="shop:define",
                files=(SourceFile(path="shop.py", content="def define(): pass\n"),),
            )
            try:
                return service.save_source(
                    self.who(f"actor-{number}", shop), source, expected_head=None
                ).revision
            except QuotaExceeded:
                return "limited"

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(save, range(8)))
        self.assertEqual(results.count("limited"), 7)
        with self.repository.connection() as db:
            for table in ("shops", "sources", "audit"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 1)

    def test_multi_process_admission_cannot_exceed_shared_pending_limit(self):
        source = self.source()
        self.policy(deployment={"pending_builds": 3})
        arguments = [
            (str(self.path), "shop-a", source.revision, "owner", f"request-{i}") for i in range(12)
        ]
        with ProcessPoolExecutor(
            max_workers=4, mp_context=multiprocessing.get_context("spawn")
        ) as pool:
            results = list(pool.map(enqueue_process, arguments))
        self.assertEqual(results.count("limited"), 9)
        with self.repository.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM builds").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM build_requests").fetchone()[0], 3)

    def test_competing_brokers_respect_global_and_identity_running_limits(self):
        for shop, actor in (
            ("shop-a", "one"),
            ("shop-b", "one"),
            ("shop-c", "two"),
            ("shop-d", "three"),
        ):
            source = self.source(shop, actor)
            self.enqueue(shop, source.revision, actor)
        with ProcessPoolExecutor(
            max_workers=4, mp_context=multiprocessing.get_context("spawn")
        ) as pool:
            claimed = list(pool.map(claim_process, [str(self.path)] * 4))
        self.assertEqual(sum(value is not None for value in claimed), 2)
        with self.repository.connection() as db:
            actors = [r[0] for r in db.execute("SELECT actor FROM builds WHERE state='running'")]
            self.assertEqual(len(set(actors)), 2)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM builds WHERE state='queued'").fetchone()[0], 2
            )

    def test_dispatch_rotates_shops_and_newcomers_do_not_jump_a_waiting_queue(self):
        a, b = self.source(), self.source("shop-b", "other")
        for _ in range(3):
            self.enqueue("shop-a", a.revision)
        first = self.repository.claim_next(60)
        self.assertEqual(first[2].shop_id, "shop-a")
        self.finish_failed(first)
        self.enqueue("shop-b", b.revision, "other")
        # Existing shop-a's turn and the newcomer now tie. Its older work wins,
        # then the waiting shop-b must run before shop-a's third job.
        second = self.repository.claim_next(60)
        self.assertEqual(second[2].shop_id, "shop-a")
        self.finish_failed(second)
        third = self.repository.claim_next(60)
        self.assertEqual(third[2].shop_id, "shop-b")
        self.finish_failed(third)
        self.assertEqual(self.repository.claim_next(60)[2].shop_id, "shop-a")

    def test_expired_worker_occupies_capacity_until_recovered(self):
        source = self.source()
        first = self.enqueue("shop-a", source.revision)
        self.enqueue("shop-a", source.revision)
        self.repository.claim(first.id, -1)
        self.assertIsNone(self.repository.claim_next(60))
        # Lowering the policy never releases existing worker reservations.
        self.policy(deployment={"running_builds": 1})
        self.assertIsNone(self.repository.claim_next(60))

    def test_denied_caller_does_not_learn_quota_or_source_state(self):
        source = self.source()
        self.policy(shop={"pending_builds": 1})
        self.enqueue("shop-a", source.revision)
        with self.assertRaises(Forbidden):
            self.service.request_build(Principal(subject="unbound"), "shop-a", source.revision)

    def test_schema_two_migration_recovers_requester_and_is_safe_to_reopen(self):
        path = self.path.parent / "legacy.db"
        source = SourceProject(
            shop_id="legacy-shop",
            entrypoint="shop:define",
            files=(SourceFile(path="shop.py", content="def define(): pass\n"),),
        )
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.execute("INSERT INTO shops(id,head) VALUES(?,?)", (source.shop_id, source.revision))
            db.execute(
                "INSERT INTO sources VALUES(?,?,?,?,?)",
                (source.shop_id, source.revision, source.to_json(), AT.isoformat(), "source-owner"),
            )
            for number in (1, 2):
                db.execute(
                    "INSERT INTO builds(id,shop_id,source_revision,state,created_at) VALUES(?,?,?,'queued',?)",
                    (f"{number:032x}", source.shop_id, source.revision, AT.isoformat()),
                )
            db.execute(
                "INSERT INTO audit(shop_id,actor,action,target,time) VALUES(?,?,?,?,?)",
                (source.shop_id, "requester", "build.requested", f"{1:032x}", AT.isoformat()),
            )
            db.execute("PRAGMA user_version=2")
        os.chmod(path, 0o600)
        with ThreadPoolExecutor(max_workers=3) as pool:
            repositories = list(pool.map(Repository, [path] * 3))
        with repositories[0].connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertEqual(
                [r[0] for r in db.execute("SELECT actor FROM builds ORDER BY id")],
                ["requester", "source-owner"],
            )
        self.assertEqual(repositories[0].read_quotas()["generation"], 0)
        self.assertIsNotNone(repositories[0].claim_next(60))


if __name__ == "__main__":
    unittest.main()
