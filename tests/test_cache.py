"""Cache reuse must never become publication, visibility, or access authority."""

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from server_support import IdentityFixture
from test_lifecycle import FixtureBuilder

from chipload.access import ACTIONS, Forbidden, Grant, Principal
from chipload.artifact import compile_shop
from chipload.cache import DataBusy, DataCache, retained_size
from chipload.lifecycle import IntegrityError, NotFound, Repository, ShopService
from chipload.project import run_cases
from chipload.published import PublishedService
from examples.heat_treatment import AT, define_shop, request


class DataCacheTests(unittest.TestCase):
    def test_byte_and_entry_limits_evict_least_recently_used(self):
        values = {key: (key * 1000).encode() for key in ("a", "b", "c")}
        weights = {
            key: retained_size(((key,), value), stop_after=100_000) + 512
            for key, value in values.items()
        }
        cache = DataCache(max_bytes=weights["a"] + weights["b"], max_entries=2)
        calls = []

        def get(key):
            def load():
                calls.append(key)
                return values[key]

            return cache.get((key,), load)

        get("a")
        get("b")
        get("a")  # b is now the least recently used entry
        get("c")
        get("a")
        self.assertEqual(calls, ["a", "b", "c"])
        get("b")
        self.assertEqual(calls, ["a", "b", "c", "b"])
        self.assertLessEqual(cache.usage["bytes"], cache.max_bytes)
        self.assertEqual(cache.usage["entries"], 2)

    def test_oversized_and_failed_loads_are_not_retained(self):
        cache = DataCache(max_bytes=1000)
        for _ in range(2):
            self.assertEqual(cache.get(("large",), lambda: b"x" * 1001), b"x" * 1001)
            with self.assertRaisesRegex(ValueError, "invalid"):
                cache.get(("failure",), lambda: (_ for _ in ()).throw(ValueError("invalid")))
        self.assertEqual(cache.usage, {"bytes": 0, "entries": 0, "loads": 0})
        self.assertEqual(cache.get(("failure",), lambda: b"recovered"), b"recovered")

    def test_concurrent_misses_share_one_load_and_hits_stay_available(self):
        cache = DataCache()
        cache.get(("warm",), lambda: b"warm")
        entered, release = threading.Event(), threading.Event()
        calls = []

        def load():
            calls.append(1)
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release loader")
            return b"shared"

        with ThreadPoolExecutor(max_workers=8) as pool:
            pending = [pool.submit(cache.get, ("cold",), load) for _ in range(8)]
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(cache.usage["loads"], 1)
                self.assertEqual(cache.get(("warm",), lambda: None), b"warm")
            finally:
                release.set()
            self.assertEqual([f.result(timeout=5) for f in pending], [b"shared"] * 8)
        self.assertEqual(len(calls), 1)
        self.assertEqual(cache.usage["loads"], 0)

    def test_saturation_and_wait_timeout_do_not_restart_a_running_load(self):
        cache = DataCache(max_loads=1, wait_seconds=0.01)
        entered, release = threading.Event(), threading.Event()
        calls = []

        def load():
            calls.append(1)
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release loader")
            return b"done"

        with ThreadPoolExecutor(max_workers=1) as pool:
            original = pool.submit(cache.get, ("one",), load)
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaisesRegex(DataBusy, "capacity"):
                    cache.get(("two",), lambda: None)
                with self.assertRaisesRegex(DataBusy, "progress"):
                    cache.get(("one",), load)
                self.assertEqual(cache.usage["loads"], 1)
            finally:
                release.set()
            self.assertEqual(original.result(timeout=5), b"done")
        self.assertEqual(cache.get(("one",), load), b"done")
        self.assertEqual(len(calls), 1)

    def test_zero_budget_disables_retention(self):
        cache = DataCache(max_bytes=0)
        calls = []
        for _ in range(2):
            cache.get(("key",), lambda: calls.append(1))
        self.assertEqual(len(calls), 2)
        self.assertEqual(cache.usage["entries"], 0)


class RepositoryCacheTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.identity = IdentityFixture(directory.name)
        self.addCleanup(self.identity.client.close)
        self.operations = self.identity.operations()
        self.writer = self.operations.service
        self.owner = self.identity.settings.bindings[0]
        self.shop = "thermal-example"
        self.repository = Repository(self.identity.settings.database)
        self.reader = ShopService(self.repository)
        self.published = PublishedService(self.reader, clock=lambda: AT)

    def publish_model(self, model):
        old = self.writer.get_source(self.owner, self.shop)
        source = old.project.model_copy(
            update={
                "files": tuple(
                    f.model_copy(update={"content": f.content + "\n# next revision\n"})
                    for f in old.project.files
                )
            }
        )
        saved = self.writer.save_source(self.owner, source, expected_head=old.revision)
        queued = self.writer.request_build(self.owner, self.shop, saved.revision)
        claim, source = self.writer.repository.claim(queued.id, 60)
        report = FixtureBuilder().build(source)
        artifact = compile_shop(model, report.artifact.identity)
        report = report.model_copy(
            update={"artifact": artifact, "tests": run_cases(artifact, source.tests)}
        )
        self.writer.repository.finish(queued.id, claim, report)
        publication = self.writer.get_publication(self.owner, self.shop)
        self.writer.publish(
            self.owner, self.shop, queued.id, expected_generation=publication.generation
        )
        return queued.id

    def test_exact_report_and_projection_are_reused_but_responses_are_independent(self):
        with patch.object(
            self.repository, "_decode_build", wraps=self.repository._decode_build
        ) as decode:
            first = self.reader.catalog(self.shop)
            original_name = first["shop"]["name"]
            first["shop"]["name"] = "caller mutation"
            first["capabilities"][0]["requirements"].clear()
            second = self.reader.catalog(self.shop)
        self.assertEqual(decode.call_count, 1)
        self.assertEqual(second["shop"]["name"], original_name)
        self.assertTrue(second["capabilities"][0]["requirements"])
        self.assertEqual(self.repository.data_cache.usage["entries"], 2)

    def test_other_repository_publication_rollback_withdraw_and_visibility_take_effect(self):
        original = self.reader.catalog(self.shop)
        first = self.writer.get_publication(self.owner, self.shop).build_id
        self.publish_model(define_shop().model_copy(update={"name": "New publication"}))
        self.assertEqual(self.reader.catalog(self.shop)["shop"]["name"], "New publication")
        self.writer.rollback(self.owner, self.shop, first, expected_generation=2)
        self.assertEqual(self.reader.catalog(self.shop), original)
        self.writer.withdraw(self.owner, self.shop, expected_generation=3)
        with self.assertRaises(NotFound):
            self.reader.catalog(self.shop)
        with self.assertRaises(NotFound):
            self.published.evaluation(self.shop, request())
        self.publish_model(define_shop().model_copy(update={"visibility": "private"}))
        with self.assertRaises(NotFound):
            self.reader.catalog(self.shop)
        with self.assertRaises(NotFound):
            self.published.evaluation(self.shop, request())

    def test_corruption_and_changed_metadata_cannot_reuse_a_warm_build(self):
        self.reader.catalog(self.shop)
        build_id = self.writer.get_publication(self.owner, self.shop).build_id
        with self.repository.connection() as db:
            payload = db.execute("SELECT report FROM builds WHERE id=?", (build_id,)).fetchone()[0]
        for sql, argument in (
            (
                "UPDATE builds SET report=? WHERE id=?",
                payload.replace(b"Thermal Processing Example", b"Tampered"),
            ),
            ("UPDATE builds SET state=? WHERE id=?", "failed"),
            ("UPDATE builds SET report=? WHERE id=?", b""),
        ):
            with self.repository.transaction() as db:
                db.execute(sql, (argument, build_id))
            with self.assertRaises(IntegrityError):
                self.reader.catalog(self.shop)
            with self.repository.transaction() as db:
                db.execute(
                    "UPDATE builds SET report=?,state='succeeded' WHERE id=?", (payload, build_id)
                )
            self.assertEqual(
                self.reader.catalog(self.shop)["shop"]["name"], "Thermal Processing Example"
            )

    def test_cached_publication_does_not_cache_observations_or_time(self):
        first = self.published.evaluation(self.shop, request())
        self.assertEqual(first["outcome"], "supported")
        snapshot = self.operations.published.read_observations(self.owner, self.shop)
        self.operations.published.write_observations(
            self.owner,
            self.shop,
            (),
            expected_revision=snapshot.revision,
        )
        missing = self.published.evaluation(self.shop, request())
        self.assertEqual(missing["outcome"], "unknown")
        self.assertIn("observation-missing", {r["code"] for r in missing["reasons"]})
        self.published.clock = lambda: AT + timedelta(days=365)
        later = self.published.evaluation(self.shop, request())
        self.assertNotEqual(missing["evaluated_at"], later["evaluated_at"])

    def test_warm_private_reads_still_require_current_shop_grants(self):
        self.published.evaluation(self.shop, request(), principal=self.owner, private=True)
        other = Principal(subject="other", grants=(Grant(shop_id="other", actions=ACTIONS),))
        for caller in (None, Principal(subject="revoked"), other):
            with self.assertRaises(Forbidden):
                self.published.evaluation(self.shop, request(), principal=caller, private=True)
            with self.assertRaises(Forbidden):
                self.published.read_evidence(caller, self.shop)

    def test_withdrawal_during_a_cold_read_blocks_subsequent_reads(self):
        entered, release = threading.Event(), threading.Event()
        decode = self.repository._decode_build

        def paused(row):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release reader")
            return decode(row)

        with patch.object(self.repository, "_decode_build", side_effect=paused):
            with ThreadPoolExecutor(max_workers=1) as pool:
                started = pool.submit(self.reader.catalog, self.shop)
                try:
                    self.assertTrue(entered.wait(2))
                    self.writer.withdraw(self.owner, self.shop, expected_generation=1)
                    with self.assertRaises(NotFound):
                        self.reader.catalog(self.shop)
                finally:
                    release.set()
                # The request that already selected the old database snapshot
                # may finish. Future requests must observe withdrawal.
                self.assertEqual(started.result(timeout=5)["shop"]["id"], self.shop)
        with self.assertRaises(NotFound):
            self.reader.catalog(self.shop)

    def test_a_corrupt_cross_shop_publication_pointer_cannot_expose_cached_private_data(self):
        build_id = self.writer.get_publication(self.owner, self.shop).build_id
        self.reader.catalog(self.shop)
        with self.repository.transaction() as db:
            db.execute("INSERT INTO shops(id,published_build) VALUES('other',?)", (build_id,))
        other = Principal(subject="other", grants=(Grant(shop_id="other", actions=ACTIONS),))
        with self.assertRaises(NotFound):
            self.published.evaluation("other", request(), principal=other, private=True)
        with self.assertRaises(NotFound):
            self.reader.catalog("other")


if __name__ == "__main__":
    unittest.main()
