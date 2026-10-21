"""Queued query authority, published snapshots, shared limits and source-answer validation."""

import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from program_support import FixtureRunner
from server_support import IdentityFixture

from chipload import BuildIdentity, compile_shop
from chipload.access import ACTIONS, Forbidden, Grant, Principal
from chipload.artifact import canonical_json
from chipload.assessment_model import AssessShop
from chipload.assessments import AssessmentService
from chipload.auth import TokenVerifier
from chipload.check_worker import invoke_checks, run_checks
from chipload.client import Client, route
from chipload.evidence import EvidenceService, RevokeEvidence
from chipload.lifecycle import Conflict, NotFound, Repository, ShopService
from chipload.operations import Operations
from chipload.program_model import RunPython
from chipload.program_runner import ProgramProcessor
from chipload.programs import ProgramService, request_for
from chipload.project import BuildReport, run_cases
from chipload.published import PublishedService
from chipload.python_checks import CaseChecks, bind_case_results
from chipload.quotas import QuotaExceeded
from chipload.sandbox import SandboxError
from chipload.server import create_app
from examples import python_shop


class CheckFixture:
    def __init__(self, environment):
        self.builder = SimpleNamespace(environment=environment)
        self.calls = 0
        self.after = lambda: None
        self.corrupt = False

    def run(self, identifier, work, **options):
        self.calls += 1
        plan = {(c.capability, c.check.id): c.check for c in work.checks}
        with patch.dict(sys.modules, {"shop": python_shop}):
            record = CaseChecks.model_validate(invoke_checks(work.query, plan, "assessment"))
        self.after()
        return record.results[:1] if self.corrupt else record.results


class AssessmentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = temp.name
        self.repository = Repository(Path(temp.name) / "shop.db")
        self.service = ShopService(self.repository)
        self.source = python_shop.project("machining")
        self.shop = self.source.shop_id
        self.owner = Principal(subject="owner", grants=(Grant(shop_id=self.shop, actions=ACTIONS),))
        self.current = self.owner
        self.at = python_shop.AT
        self.environment = SimpleNamespace(sdk_sha256="a" * 64, dependencies_sha256="b" * 64,
                                           python_version="3.12", image="sha256:" + "c" * 64)
        self.publish()
        self.assessments = AssessmentService(self.repository, clock=lambda: self.at)
        self.operations = Operations(self.service, PublishedService(self.service, clock=lambda: self.at))
        self.runner = FixtureRunner()
        self.checks = CheckFixture(self.environment)
        self.processor = ProgramProcessor(self.repository, self.runner, lambda _: self.current,
                                          clock=lambda: self.at, operations=self.operations,
                                          assessment_runner=self.checks)

    def publish(self, *, hidden=False):
        shop = python_shop.define_shop()
        if hidden:
            cap = shop.capabilities[0]
            shop = shop.model_copy(update={"capabilities": (cap.model_copy(update={
                "python_checks": tuple(c.model_copy(update={"visibility": "private"}) for c in cap.python_checks)
            }),)})
        env = self.environment
        self.artifact = compile_shop(shop, BuildIdentity(source_sha256=self.source.revision,
            sdk_sha256=env.sdk_sha256, dependencies_sha256=env.dependencies_sha256,
            runtime=f"CPython-{env.python_version};docker-{env.image}"))
        with patch.dict(sys.modules, {"shop": python_shop}):
            contexts = bind_case_results(self.artifact, self.source.tests, run_checks(shop, self.source))
        head = self.service.get_publication(self.owner, self.shop) if hasattr(self, "build") else None
        if head is None:
            self.service.save_source(self.owner, self.source, expected_head=None)
        self.build = self.service.request_build(self.owner, self.shop, self.source.revision)
        claim, _ = self.repository.claim(self.build.id, 30)
        self.repository.finish(self.build.id, claim, BuildReport(
            source_revision=self.source.revision, worker_image=env.image, status="succeeded",
            artifact=self.artifact, tests=run_cases(self.artifact, self.source.tests, python_evaluations=contexts)))
        self.service.publish(self.owner, self.shop, self.build.id, expected_generation=head.generation if head else 0)
        self.revision = self.service.catalog(self.shop)["public_sha256"]

    def request(self, key="one", case=0, **changes):
        return AssessShop(shop_id=self.shop, request_key=key, query=self.source.tests[case].request,
                          expected_revision=self.revision, **changes)

    def read(self, job):
        return self.assessments.read(self.owner, self.shop, job.id)

    def test_real_function_outcomes_and_exact_retries_have_no_source_authority(self):
        for index, case in enumerate(self.source.tests):
            request = self.request(case.id, index)
            job = self.assessments.submit(self.owner, request)
            self.assertEqual(self.assessments.submit(self.owner, request).id, job.id)
            self.assertTrue(self.processor.run_next())
            receipt = self.read(job)
            self.assertEqual(receipt.state, "succeeded")
            self.assertEqual(receipt.evaluation["outcome"], case.expected)
            self.assertNotIn("model_sha256", receipt.evaluation)
            self.assertNotIn("entrypoint", canonical_json(receipt).decode())
            self.assertEqual(self.assessments.submit(self.owner, request), receipt)
            with self.assertRaises(NotFound):
                ProgramService(self.repository).read(self.owner, self.shop, job.id)
        with self.assertRaises(Conflict):
            self.assessments.submit(self.owner, self.request(self.source.tests[0].id, 1))

    def test_grants_actor_isolation_and_private_queries(self):
        reader = Principal(subject="owner", grants=(Grant(shop_id=self.shop, actions=("evaluation:run",)),))
        job = self.assessments.submit(reader, self.request())
        stranger = reader.model_copy(update={"subject": "stranger"})
        for call in (self.assessments.read, self.assessments.cancel):
            with self.assertRaises(NotFound):
                call(stranger, self.shop, job.id)
        with self.assertRaises(Forbidden):
            self.assessments.submit(reader, self.request("private", private=True))
        self.current = None
        self.processor.run_next()
        self.assertEqual(self.read(job).state, "failed")
        self.assertEqual(self.checks.calls, 0)
        self.current = self.owner
        private = self.request("private", private=True).model_copy(update={"expected_revision": self.artifact.model_sha256})
        job = self.assessments.submit(self.owner, private)
        self.processor.run_next()
        self.assertEqual(self.read(job).evaluation["model_sha256"], self.artifact.model_sha256)
        with self.assertRaises(Forbidden):
            self.assessments.read(reader, self.shop, job.id)
        # Cancellation carries no private result, including for terminal jobs.
        self.assertIsNone(self.assessments.cancel(reader, self.shop, job.id).evaluation)

    def test_concurrent_retries_and_server_selected_publication_pins_admit_once(self):
        with ThreadPoolExecutor(4) as pool:
            jobs = list(pool.map(lambda _: self.assessments.submit(self.owner, self.request()), range(8)))
        self.assertEqual(len({job.id for job in jobs}), 1)
        with self.repository.connection() as db:
            task = request_for(db.execute("SELECT * FROM python_runs WHERE id=?", (jobs[0].id,)).fetchone())
        selected_later = task.model_copy(update={"generation": task.generation + 1})
        prior = self.assessments.programs.enqueue(self.owner, selected_later, kind="assessment")
        self.assertEqual(prior.id, jobs[0].id)

    def test_environment_mismatch_and_grant_revocation_after_execution_withhold_results(self):
        environment = self.checks.builder.environment
        self.checks.builder.environment = SimpleNamespace(**(vars(environment) | {"sdk_sha256": "0" * 64}))
        job = self.assessments.submit(self.owner, self.request())
        self.processor.run_next()
        self.assertEqual(self.checks.calls, 0)
        self.assertIsNone(self.read(job).evaluation)
        self.checks.builder.environment = environment
        self.checks.after = lambda: setattr(self, "current", None)
        job = self.assessments.submit(self.owner, self.request("revoked"))
        self.processor.run_next()
        self.assertEqual(self.checks.calls, 1)
        self.assertEqual(self.read(job).state, "failed")
        self.assertIsNone(self.read(job).evaluation)

    def test_opaque_public_conditions_do_not_invoke_source(self):
        self.publish(hidden=True)
        job = self.assessments.submit(self.owner, self.request())
        self.processor.run_next()
        receipt = self.read(job)
        self.assertEqual(receipt.state, "succeeded")
        self.assertEqual(receipt.evaluation["outcome"], "unknown")
        self.assertEqual(receipt.evaluation["reasons"][0]["code"], "unpublished-condition")
        self.assertEqual(self.checks.calls, 0)

    def test_publication_fence_and_same_build_republication_supersede_receipts(self):
        job = self.assessments.submit(self.owner, self.request())
        self.processor.run_next()
        self.service.withdraw(self.owner, self.shop, expected_generation=1)
        self.assertEqual(self.read(job).state, "superseded")
        self.assertIsNone(self.read(job).evaluation)
        self.service.publish(self.owner, self.shop, self.build.id, expected_generation=2)
        self.assertEqual(self.read(job).state, "superseded")
        pending = self.assessments.submit(self.owner, self.request("pending"))
        self.checks.after = lambda: self.service.withdraw(self.owner, self.shop, expected_generation=3)
        self.processor.run_next()
        self.assertNotEqual(self.read(pending).state, "succeeded")
        self.assertIsNone(self.read(pending).evaluation)

    def test_observation_changes_and_evidence_revocation_withhold_old_answers(self):
        job = self.assessments.submit(self.owner, self.request())
        self.processor.run_next()
        self.operations.published.write_observations(self.owner, self.shop, (), expected_revision=None)
        self.assertEqual(self.read(job).state, "superseded")
        current = self.assessments.submit(self.owner, self.request("new"))
        self.processor.run_next()
        EvidenceService(self.repository, clock=lambda: self.at).revoke(self.owner, RevokeEvidence(
            shop_id=self.shop, evidence_id="declaration", expected_model_sha256=self.artifact.model_sha256,
            reason="qualification withdrawn"))
        self.assertEqual(self.read(current).state, "superseded")
        new = self.assessments.submit(self.owner, self.request("revoked"))
        self.processor.run_next()
        self.assertEqual(self.read(new).evaluation["outcome"], "unknown")

    def test_cancellation_and_invalid_worker_answers_never_become_domain_results(self):
        job = self.assessments.submit(self.owner, self.request())
        self.assessments.cancel(self.owner, self.shop, job.id)
        self.assertFalse(self.processor.run_next())
        running = self.assessments.submit(self.owner, self.request("running"))
        self.checks.after = lambda: self.assessments.cancel(self.owner, self.shop, running.id)
        self.processor.run_next()
        self.assertEqual(self.read(running).state, "cancelled")
        self.checks.after = lambda: None
        self.checks.corrupt = True
        broken = self.assessments.submit(self.owner, self.request("broken"))
        self.processor.run_next()
        self.assertEqual(self.read(broken).state, "interrupted")
        self.assertIsNone(self.read(broken).evaluation)

    def test_shared_quota_and_unconfirmed_cleanup_keep_capacity(self):
        policy = self.repository.policy()
        policy = policy.model_copy(update={"shop": policy.shop.model_copy(update={"pending_programs": 1})})
        self.repository.set_quotas(policy, expected_generation=0)
        job = self.assessments.submit(self.owner, self.request())
        with self.assertRaises(QuotaExceeded):
            ProgramService(self.repository).submit(self.owner, RunPython(shop_id=self.shop, request_key="code", code="result=1"))
        self.checks.corrupt = True
        with patch.object(self.runner, "remove", side_effect=SandboxError("cleanup unavailable")):
            with self.assertRaises(SandboxError):
                self.processor.run_next()
        self.assertEqual(self.read(job).state, "running")
        self.at += timedelta(minutes=2)
        self.assertEqual(self.processor.recover_expired(), 1)
        self.assertEqual(self.read(job).state, "interrupted")

    def test_schema_ten_migrates_concurrently_without_resetting_queue_or_capacity(self):
        code = ProgramService(self.repository).submit(self.owner, RunPython(shop_id=self.shop, request_key="old", code="result=1"))
        with self.repository.transaction() as db:
            db.execute("ALTER TABLE python_runs DROP COLUMN kind")
            db.execute("PRAGMA user_version=10")
        from chipload import lifecycle

        with patch.object(lifecycle, "ASSESSMENT_MIGRATION", lifecycle.ASSESSMENT_MIGRATION + "INVALID SQL;"):
            with self.assertRaises(sqlite3.OperationalError):
                Repository(self.repository.path)
        with self.repository.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 10)
            self.assertNotIn("kind", {row["name"] for row in db.execute("PRAGMA table_info(python_runs)")})
        with ThreadPoolExecutor(4) as pool:
            repositories = list(pool.map(lambda _: Repository(self.repository.path), range(8)))
        self.assertTrue(all(r.admission.identity == self.repository.admission.identity for r in repositories))
        self.assertEqual(ProgramService(self.repository).read(self.owner, self.shop, code.id).state, "queued")
        with self.repository.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 11)
            self.assertEqual(db.execute("SELECT kind FROM python_runs WHERE id=?", (code.id,)).fetchone()[0], "program")

    def test_rest_and_python_share_results_and_permissions(self):
        identity = IdentityFixture(self.directory)
        self.addCleanup(identity.client.close)
        settings = identity.settings.model_copy(update={"database": self.repository.path, "bindings": (self.owner,)})
        verifier = TokenVerifier(settings, client=identity.client)
        with TestClient(create_app(settings, operations=self.operations, verifier=verifier), base_url="http://localhost:8088") as http:
            headers = {"Authorization": "Bearer " + identity.token()}
            path = f"/v1/shops/{self.shop}/assessments"
            request = self.request().model_dump(mode="json")
            self.assertEqual(http.post(path, json=request).status_code, 401)
            self.assertEqual(http.post(path, json={**request, "build_id": self.build.id}, headers=headers).status_code, 422)
            response = http.post(path, json=request, headers=headers)
            self.assertEqual(response.status_code, 202, response.text)
            job = response.json()
            self.processor.run_next()
            client = Client(lambda op, body: self.operations.call(op, self.owner, body)).shop(self.shop)
            self.assertEqual(http.get(path + "/" + job["id"], headers=headers).json(), client.assessment(job["id"]))
            method, url, body = route("assessment_cancel", {"shop_id": self.shop, "assessment_id": job["id"]})
            self.assertEqual((method, url, body), ("POST", path + "/" + job["id"] + "/cancel", None))


if __name__ == "__main__":
    unittest.main()
