"""Durable code admission, least authority, journals and recovery without source execution."""

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from program_support import SCRIPT, FixtureRunner
from server_support import IdentityFixture

from chipload.access import ACTIONS, Forbidden, Grant, Principal
from chipload.artifact import canonical_json
from chipload.lifecycle import Conflict, IntegrityError, Repository, now
from chipload.program_model import RunPython
from chipload.program_runner import ProgramProcessor
from chipload.programs import ProgramService, intersect
from chipload.quotas import QuotaExceeded, QuotaPolicy
from chipload.sandbox import SandboxError


class ProgramTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.identity = IdentityFixture(temp.name)
        self.addCleanup(self.identity.client.close)
        self.operations = self.identity.operations()
        self.repository = self.operations.service.repository
        self.owner = self.identity.settings.bindings[0]
        self.current = self.owner
        self.at = now()
        self.service = ProgramService(self.repository, clock=lambda: self.at)
        self.runner = FixtureRunner()
        self.processor = ProgramProcessor(
            self.repository,
            self.runner,
            lambda _: self.current,
            clock=lambda: self.at,
            operations=self.operations,
        )

    def request(self, key="one", operation="source_read", **body):
        return RunPython(
            shop_id="thermal-example",
            request_key=key,
            code=SCRIPT,
            inputs={"operation": operation, "request": {"shop_id": "thermal-example", **body}},
        )

    def read(self, job):
        return self.service.read(self.owner, job.shop_id, job.id)

    def test_competing_exact_retries_admit_once_and_conflicting_key_fails(self):
        request = self.request()
        with ThreadPoolExecutor(4) as pool:
            jobs = list(pool.map(lambda _: self.service.submit(self.owner, request), range(8)))
        self.assertEqual(len({j.id for j in jobs}), 1)
        with self.assertRaises(Conflict):
            self.service.submit(self.owner, request.model_copy(update={"code": "result = 9"}))
        self.assertTrue(self.processor.run_next())
        self.assertFalse(self.processor.run_next())
        result = self.read(jobs[0])
        self.assertEqual(result.state, "succeeded")
        self.assertTrue(result.value["ok"])
        self.assertEqual(result.calls[0].state, "returned")
        self.assertEqual(self.service.submit(self.owner, request), result)

    def test_scope_ceiling_prevents_cross_shop_and_later_grant_expansion(self):
        self.current = Principal(
            subject="owner", grants=(*self.owner.grants, Grant(shop_id="other", actions=ACTIONS))
        )
        job = self.service.submit(self.current, self.request(shop_id="other"))
        self.processor.run_next()
        self.assertEqual(self.read(job).value["status"], 403)
        restricted = Principal(
            subject="owner",
            grants=(Grant(shop_id="thermal-example", actions=("code:run", "source:read")),),
        )
        job = self.service.submit(restricted, self.request("evidence", "evidence_read"))
        self.processor.run_next()
        self.assertEqual(self.read(job).value["status"], 403)
        left = Principal(
            subject="owner",
            grants=(
                Grant(
                    shop_id="thermal-example",
                    actions=("operation:execute",),
                    operations=("one", "two"),
                ),
            ),
        )
        right = Principal(
            subject="owner",
            grants=(
                Grant(shop_id="thermal-example", actions=ACTIONS, operations=("two", "three")),
            ),
        )
        self.assertEqual(intersect(left, right).grants[0].operations, ("two",))

    def test_revocation_checked_before_execution_and_again_for_each_call(self):
        job = self.service.submit(self.owner, self.request())
        self.current = None
        self.processor.run_next()
        self.assertEqual(self.read(job).state, "failed")
        self.current = self.owner
        self.service.submit(self.owner, self.request("two"))
        row, _ = self.processor.claim_next()
        self.assertTrue(
            self.processor.dispatch(row, "source_read", {"shop_id": "thermal-example"})["ok"]
        )
        self.current = None
        with self.assertRaises(Forbidden):
            self.processor.dispatch(row, "source_read", {"shop_id": "thermal-example"})

    def test_partial_commit_and_lost_completion_never_replay(self):
        source = self.operations.service.get_source(self.owner, "thermal-example")
        body = {
            "shop_id": "thermal-example",
            "revision": source.revision,
            "request_key": "inner-build",
        }
        job = self.service.submit(
            self.owner,
            self.request(
                operation="build_request", revision=source.revision, request_key="inner-build"
            ),
        )
        row, _ = self.processor.claim_next()
        original = self.operations.call

        def commit_then_crash(name, who, request):
            original(name, who, request)
            raise SystemExit("process died after service commit")

        with patch.object(self.operations, "call", commit_then_crash):
            with self.assertRaises(SystemExit):
                self.processor.dispatch(row, "build_request", body)
        self.assertEqual(self.read(job).calls[0].state, "started")
        self.at += timedelta(minutes=2)
        self.assertEqual(self.processor.recover_expired(), 1)
        result = self.read(job)
        self.assertEqual(result.state, "interrupted")
        self.assertEqual(
            self.service.submit(
                self.owner,
                self.request(
                    operation="build_request", revision=source.revision, request_key="inner-build"
                ),
            ).id,
            job.id,
        )
        self.assertFalse(self.processor.run_next())
        with self.repository.connection() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM build_requests WHERE request_key='inner-build'"
                ).fetchone()[0],
                1,
            )

    def test_cancel_stops_future_calls_but_keeps_returned_journal(self):
        job = self.service.submit(self.owner, self.request())
        row, _ = self.processor.claim_next()
        self.processor.dispatch(row, "source_read", {"shop_id": "thermal-example"})
        self.service.cancel(self.owner, job.shop_id, job.id)
        self.assertFalse(self.processor.active(job.id, row["claim"]))
        with self.assertRaises(SandboxError):
            self.processor.dispatch(row, "source_read", {"shop_id": "thermal-example"})
        self.processor.finish(row, {"error": "cancelled"}, "interrupted")
        self.assertEqual(self.read(job).state, "cancelled")
        self.assertEqual(self.read(job).calls[0].state, "returned")
        other = self.service.submit(self.owner, self.request("queued"))
        self.assertEqual(
            self.service.cancel(self.owner, other.shop_id, other.id).state, "cancelled"
        )
        self.assertFalse(self.processor.run_next())

    def test_expired_claim_fenced_and_cleanup_failure_retains_capacity(self):
        job = self.service.submit(self.owner, self.request())
        row, _ = self.processor.claim_next()
        self.at += timedelta(minutes=2)
        with self.assertRaises(Conflict):
            self.processor.finish(row, {"value": "late"}, "succeeded")
        with patch.object(self.runner, "remove", side_effect=SandboxError("daemon unavailable")):
            with self.assertRaises(SandboxError):
                self.processor.recover_expired()
        self.assertEqual(self.read(job).state, "running")
        self.at += timedelta(minutes=1)
        self.assertEqual(self.processor.recover_expired(), 1)
        with self.assertRaises(Conflict):
            self.processor.finish(row, {"value": "stale"}, "succeeded")

    def test_quotas_are_separate_from_builds_and_apply_to_retained_runs(self):
        quota = self.repository.read_quotas()
        quota["policy"]["shop"]["pending_programs"] = 1
        self.repository.set_quotas(
            QuotaPolicy.model_validate(quota["policy"]), expected_generation=quota["generation"]
        )
        job = self.service.submit(self.owner, self.request())
        with self.assertRaises(QuotaExceeded):
            self.service.submit(self.owner, self.request("two"))
        self.processor.run_next()
        self.service.submit(self.owner, self.request("two"))
        self.assertEqual(self.service.submit(self.owner, self.request()).id, job.id)
        with self.assertRaises(Forbidden):
            self.service.read(Principal(subject="other"), job.shop_id, job.id)

    def test_invalid_source_paths_and_corrupt_persistence_fail_closed(self):
        for path in ("../evil.py", "main.py", "chipload.py"):
            with self.assertRaises(ValueError):
                RunPython(
                    shop_id="thermal-example",
                    request_key="one",
                    code="result=1",
                    files=({"path": path, "content": "x"},),
                )
        job = self.service.submit(self.owner, self.request())
        with self.repository.transaction() as db:
            db.execute(
                "UPDATE python_runs SET ceiling=? WHERE id=?",
                (canonical_json(Principal(subject="other")), job.id),
            )
        with self.assertRaises(IntegrityError):
            self.read(job)
        with self.assertRaises(IntegrityError):
            self.processor.claim_next()

    def test_running_capacity_and_sdk_call_limit_hold_across_processors(self):
        one = self.service.submit(self.owner, self.request())
        self.at += timedelta(milliseconds=1)
        two = self.service.submit(self.owner, self.request("two"))
        row, _ = self.processor.claim_next()
        other = ProgramProcessor(
            self.repository, self.runner, lambda _: self.owner, operations=self.operations
        )
        self.assertIsNone(other.claim_next())
        for _ in range(64):
            self.processor.dispatch(row, "source_read", {"shop_id": one.shop_id})
        with self.assertRaises(SandboxError):
            self.processor.dispatch(row, "source_read", {"shop_id": one.shop_id})
        self.processor.finish(row, {"value": 1}, "succeeded")
        next_row, _ = other.claim_next()
        self.assertEqual(next_row["id"], two.id)
        self.assertEqual(len(self.read(one).calls), 64)

    def test_schema_six_migrates_atomically_without_resetting_admission(self):
        with self.repository.transaction() as db:
            db.execute("DROP TABLE python_runs")
            db.execute("PRAGMA user_version=6")
        from chipload import lifecycle

        with patch.object(lifecycle, "PROGRAM_SCHEMA", lifecycle.PROGRAM_SCHEMA + "INVALID SQL;"):
            with self.assertRaises(Exception):
                Repository(self.repository.path)
        with self.repository.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
        fresh = Repository(self.repository.path)
        self.assertEqual(fresh.admission.identity, self.repository.admission.identity)
        with fresh.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 7)
