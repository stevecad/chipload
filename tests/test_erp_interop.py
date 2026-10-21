"""External record provenance, freshness, scoped reads, and observation synchronization."""

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from erp_support import ERPFixture

from chipload import ExternalReference, Observation, quantity
from chipload.client import Client, ServiceError, TransportError
from chipload.execution_model import ExecutionRecord
from chipload.project import run_cases
from examples import erp_shop


class ERPInteropTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.f = ERPFixture(Path(temp.name) / "shop.db")

    def test_domain_matrix_and_external_reference_are_private(self):
        f = self.f
        self.assertTrue(all(case.passed for case in run_cases(f.build.report.artifact, erp_shop.cases())))
        receipt = f.read_stock()
        self.assertEqual(erp_shop.sync_stock(f.shop, receipt["id"])["state"], "updated")
        observation = f.shop.observations()["observations"][0]
        self.assertEqual(observation["external"], {"system": erp_shop.SYSTEM, "record": erp_shop.STOCK_RECORD, "revision": "stock-v1"})
        self.assertEqual(Observation.model_validate(observation).time, erp_shop.AT)
        self.assertEqual(observation["source"], "simulated")
        self.assertEqual(f.shop.evaluate(erp_shop.request())["outcome"], "supported")
        public = str(f.shop.catalog()) + str(f.shop.evaluate(erp_shop.request()))
        self.assertNotIn(erp_shop.STOCK_RECORD, public)
        self.assertNotIn(erp_shop.SYSTEM, public)
        self.assertNotIn("stock-v1", public)
        for forbidden in ("source_read", "publication_read", "evidence_read"):
            with self.assertRaises(ServiceError) as error:
                f.call(forbidden, {"shop_id": erp_shop.SHOP})
            self.assertEqual(error.exception.status, 403)

    def test_retry_keeps_upstream_time_and_does_not_duplicate_reads(self):
        f = self.f
        receipt = f.read_stock()
        first = erp_shop.sync_stock(f.shop, receipt["id"])
        f.at += timedelta(seconds=61)
        self.assertEqual(f.read_stock(), receipt)
        repeated = erp_shop.sync_stock(f.shop, receipt["id"])
        self.assertEqual(repeated, {"state": "unchanged", "revision": first["revision"]})
        result = f.shop.evaluate(erp_shop.request())
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(result["freshness"][0]["state"], "stale")
        self.assertEqual(f.shop.evaluate(erp_shop.request(purpose="capability"))["outcome"], "supported")
        with f.repository.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM simulation_effects").fetchone()[0], 1)

    def test_out_of_order_and_inconsistent_versions_cannot_replace_newer_data(self):
        f = self.f
        older = f.read_stock()
        f.at += timedelta(seconds=5)
        f.adapter.outputs = f.outputs(version="stock-v2", count="0")
        newer = f.read_stock("newer")
        erp_shop.sync_stock(f.shop, newer["id"])
        self.assertEqual(f.shop.evaluate(erp_shop.request())["outcome"], "conditional")
        self.assertEqual(erp_shop.sync_stock(f.shop, older["id"])["state"], "ignored-older")
        f.adapter.outputs = f.outputs(version="stock-v2", count="5")
        inconsistent = f.read_stock("inconsistent")
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            erp_shop.sync_stock(f.shop, inconsistent["id"])
        self.assertFalse(f.shop.observations()["observations"][0]["value"])

    def test_unknown_and_failed_reads_do_not_invent_fresh_stock(self):
        f = self.f
        f.adapter.outputs = f.outputs(count=None)
        job = f.read_stock()
        erp_shop.sync_stock(f.shop, job["id"])
        self.assertIsNone(f.shop.observations()["observations"][0]["value"])
        self.assertEqual(f.shop.evaluate(erp_shop.request())["outcome"], "unknown")
        before = f.shop.observations()
        f.adapter.outputs = ()  # Invalid external response remains indeterminate.
        failed = f.read_stock("failed")
        self.assertEqual(failed["state"], "indeterminate")
        with self.assertRaises(ValueError):
            erp_shop.sync_stock(f.shop, failed["id"])
        self.assertEqual(f.shop.observations(), before)

    def test_mapper_rejects_wrong_records_units_times_and_quantities(self):
        receipt = self.f.read_stock()
        for field, bad in (("record-reference", "another-record"), ("record-revision", " "),
                           ("observed-at", "2026-10-20T12:00:00"),
                           ("available-units", quantity("-1", "1").model_dump()),
                           ("available-units", quantity("0.5", "1").model_dump()),
                           ("available-units", quantity("1", "kg").model_dump())):
            value = ExecutionRecord.model_validate(receipt).model_dump(mode="json")
            value["result"]["outputs"] = [{**fact, "value": bad} if fact["name"] == field else fact for fact in value["result"]["outputs"]]
            with self.subTest(field=field, value=bad), self.assertRaises(ValueError):
                erp_shop.stock_observation(value)
        self.assertIsNone(ExternalReference(system="external", record="item").revision)

    def test_conflicting_writers_merge_without_erasing_unrelated_observations(self):
        f = self.f
        job = f.read_stock()
        other = erp_shop.sample_observation(id="other-observation", external=None)
        writes = 0

        def racing(operation, request):
            nonlocal writes
            if operation == "observations_write":
                writes += 1
                if writes == 1:
                    f.shop.set_observations((other,), expected_revision=None)
            return f.call(operation, request)

        merged = Client(racing).shop(erp_shop.SHOP)
        erp_shop.sync_stock(merged, job["id"])
        self.assertEqual(writes, 2)
        self.assertEqual({o["id"] for o in f.shop.observations()["observations"]}, {other.id, erp_shop.OBSERVATION})

    def test_lost_write_response_reconciles_without_another_write(self):
        f = self.f
        job = f.read_stock()
        writes = 0

        def lost(operation, request):
            nonlocal writes
            result = f.call(operation, request)
            if operation == "observations_write":
                writes += 1
                raise TransportError("response lost after commit")
            return result

        client = Client(lost).shop(erp_shop.SHOP)
        with self.assertRaises(TransportError):
            erp_shop.sync_stock(client, job["id"])
        self.assertEqual(erp_shop.sync_stock(client, job["id"])["state"], "unchanged")
        self.assertEqual(writes, 1)

    def test_source_ownership_and_changed_binding_refuse_synchronization(self):
        f = self.f
        job = f.read_stock()
        f.shop.set_observations((erp_shop.sample_observation(external=None),), expected_revision=None)
        with self.assertRaisesRegex(ValueError, "another source"):
            erp_shop.sync_stock(f.shop, job["id"])
        f.operations.executions.bind(f.binding, expected_generation=1)
        with self.assertRaisesRegex(ValueError, "binding changed"):
            erp_shop.sync_stock(f.shop, job["id"])

    def test_snapshot_limits_reject_large_external_metadata_before_persistence(self):
        f = self.f
        existing = f.shop.observations()
        reference = ExternalReference(system="erp", record="😀" * 512, revision="v1")
        observations = tuple(erp_shop.sample_observation(id=f"item-{number}", external=reference) for number in range(1000))
        with self.assertRaises(ValueError):
            f.operations.published.write_observations(f.agent, erp_shop.SHOP, observations, expected_revision=None)
        self.assertEqual(f.shop.observations(), existing)


if __name__ == "__main__":
    unittest.main()
