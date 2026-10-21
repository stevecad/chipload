import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from chipload.access import ACTIONS, Forbidden, Grant, Principal
from chipload.artifact import BuildIdentity, compile_shop
from chipload.lifecycle import BuildProcessor, Conflict, NotFound, Repository, ShopService
from chipload.project import BuildReport, run_cases
from chipload.sandbox import SandboxError, WorkerEnvironment
from examples.export_project import project
from examples.heat_treatment import define_shop


class FixtureBuilder:
    """Storage/service test double. Real execution boundaries are tested separately."""

    seconds = 1
    environment = WorkerEnvironment("sha256:" + "a" * 64, "b" * 64, "c" * 64, "3.12.15")

    def build(self, source, *, job_id=None, guard=None):
        shop = define_shop()
        if "Updated thermal example" in source.files[0].content:
            shop = shop.model_copy(update={"name": "Updated thermal example"})
        artifact = compile_shop(
            shop,
            BuildIdentity(
                source_sha256=source.revision,
                dependencies_sha256=self.environment.dependencies_sha256,
                sdk_sha256=self.environment.sdk_sha256,
                runtime="test-fixture",
            ),
        )
        tests = run_cases(artifact, source.tests)
        return BuildReport(
            source_revision=source.revision,
            worker_image=self.environment.image,
            artifact=artifact,
            tests=tests,
            status="succeeded" if all(t.passed for t in tests) else "failed",
        )

    def remove(self, job_id, *, guard=None):
        self.removed = job_id


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "repository.db"
        self.repository = Repository(self.path)
        self.service = ShopService(self.repository)
        self.builder = FixtureBuilder()
        self.processor = BuildProcessor(self.repository, self.builder)
        self.project = project()
        self.shop = self.project.shop_id
        self.owner = Principal(subject="owner", grants=(Grant(shop_id=self.shop, actions=ACTIONS),))

    def build(self, source=None, *, expected=None):
        source = source or self.project
        saved = self.service.save_source(self.owner, source, expected_head=expected)
        record = self.service.request_build(self.owner, self.shop, saved.revision)
        self.processor.run(record.id)
        return self.service.get_build(self.owner, self.shop, record.id)

    def changed(self):
        file = self.project.files[0]
        return self.project.model_copy(
            update={
                "files": (
                    file.model_copy(
                        update={
                            "content": file.content.replace(
                                "Thermal Processing Example", "Updated thermal example"
                            )
                        }
                    ),
                )
            }
        )

    def test_author_build_test_publish_edit_rollback_withdraw(self):
        first = self.build()
        with self.assertRaises(NotFound):
            self.service.catalog(self.shop)
        self.service.publish(self.owner, self.shop, first.id, expected_generation=0)
        catalog = self.service.catalog(self.shop)
        self.assertEqual(catalog["shop"]["name"], "Thermal Processing Example")
        second = self.build(self.changed(), expected=first.source_revision)
        self.assertEqual(self.service.catalog(self.shop), catalog)
        self.service.publish(self.owner, self.shop, second.id, expected_generation=1)
        self.assertEqual(self.service.catalog(self.shop)["shop"]["name"], "Updated thermal example")
        self.service.rollback(self.owner, self.shop, first.id, expected_generation=2)
        self.assertEqual(self.service.catalog(self.shop), catalog)
        self.service.withdraw(self.owner, self.shop, expected_generation=3)
        with self.assertRaises(NotFound):
            self.service.catalog(self.shop)
        self.assertEqual(self.service.get_publication(self.owner, self.shop).generation, 4)

    def test_failed_tests_and_empty_test_suites_cannot_replace_publication(self):
        first = self.build()
        self.service.publish(self.owner, self.shop, first.id, expected_generation=0)
        cases = self.project.tests
        failing = self.project.model_copy(
            update={"tests": (cases[0].model_copy(update={"expected": "unsupported"}),) + cases[1:]}
        )
        failed = self.build(failing, expected=self.project.revision)
        self.assertEqual(failed.state, "failed")
        with self.assertRaises(Conflict):
            self.service.publish(self.owner, self.shop, failed.id, expected_generation=1)
        empty = self.project.model_copy(update={"tests": ()})
        untested = self.build(empty, expected=failing.revision)
        with self.assertRaises(Conflict):
            self.service.publish(self.owner, self.shop, untested.id, expected_generation=1)
        self.assertEqual(self.service.get_publication(self.owner, self.shop).build_id, first.id)

    def test_concurrent_edits_have_one_winner(self):
        self.service.save_source(self.owner, self.project, expected_head=None)
        changed = self.changed()
        alternate = changed.model_copy(
            update={
                "files": (
                    changed.files[0].model_copy(
                        update={"content": changed.files[0].content + "\n"}
                    ),
                )
            }
        )

        def save(candidate):
            try:
                return self.service.save_source(
                    self.owner, candidate, expected_head=self.project.revision
                ).revision
            except Conflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, [changed, alternate]))
        self.assertEqual(results.count("conflict"), 1)
        head = self.service.get_source(self.owner, self.shop).revision
        self.assertIn(head, {changed.revision, alternate.revision})
        self.assertEqual(
            self.service.get_source(self.owner, self.shop, self.project.revision).project,
            self.project,
        )

    def test_publication_generation_prevents_stale_updates_even_after_rollback(self):
        first = self.build()
        self.service.publish(self.owner, self.shop, first.id, expected_generation=0)
        self.service.withdraw(self.owner, self.shop, expected_generation=1)
        self.service.rollback(self.owner, self.shop, first.id, expected_generation=2)
        with self.assertRaises(Conflict):
            self.service.withdraw(self.owner, self.shop, expected_generation=1)

    def test_unpublished_build_is_not_a_rollback_target(self):
        first = self.build()
        with self.assertRaises(Conflict):
            self.service.rollback(self.owner, self.shop, first.id, expected_generation=0)

    def test_authoring_does_not_grant_publication(self):
        editor = Principal(
            subject="editor",
            grants=(
                Grant(
                    shop_id=self.shop,
                    actions=("source:read", "source:write", "build:read", "build:run"),
                ),
            ),
        )
        saved = self.service.save_source(editor, self.project, expected_head=None)
        build = self.service.request_build(editor, self.shop, saved.revision)
        self.processor.run(build.id)
        with self.assertRaises(Forbidden):
            self.service.publish(editor, self.shop, build.id, expected_generation=0)
        with self.assertRaises(Forbidden):
            self.service.audit(editor, self.shop)

    def test_cross_shop_and_anonymous_access_is_denied_before_lookup(self):
        first = self.build()
        stranger = Principal(subject="stranger", grants=(Grant(shop_id="other", actions=ACTIONS),))
        for who in (stranger, None):
            for operation in (
                lambda: self.service.get_source(who, self.shop),
                lambda: self.service.get_build(who, self.shop, first.id),
                lambda: self.service.request_build(who, self.shop, first.source_revision),
                lambda: self.service.save_source(who, self.project, expected_head=None),
                lambda: self.service.publish(who, self.shop, first.id, expected_generation=0),
                lambda: self.service.withdraw(who, self.shop, expected_generation=0),
                lambda: self.service.audit(who, self.shop),
            ):
                with self.assertRaises(Forbidden):
                    operation()
        with self.assertRaises(NotFound):
            self.service.get_build(stranger, "other", first.id)

    def test_reopening_preserves_queued_work_and_published_revisions(self):
        saved = self.service.save_source(self.owner, self.project, expected_head=None)
        queued = self.service.request_build(self.owner, self.shop, saved.revision)
        reopened = Repository(self.path)
        BuildProcessor(reopened, self.builder).run(queued.id)
        restarted = ShopService(reopened)
        restarted.publish(self.owner, self.shop, queued.id, expected_generation=0)
        self.assertEqual(self.service.catalog(self.shop), restarted.catalog(self.shop))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        actions = [e["action"] for e in restarted.audit(self.owner, self.shop)]
        self.assertEqual(
            actions,
            [
                "source.saved",
                "build.requested",
                "build.started",
                "build.succeeded",
                "publication.publish",
            ],
        )

    def test_a_build_cannot_be_claimed_or_finished_twice(self):
        saved = self.service.save_source(self.owner, self.project, expected_head=None)
        queued = self.service.request_build(self.owner, self.shop, saved.revision)
        claim, source = self.repository.claim(queued.id, 60)
        report = self.builder.build(source)
        with self.assertRaises(Conflict):
            self.repository.claim(queued.id, 60)
        with self.assertRaises(Conflict):
            self.repository.finish(queued.id, "wrong-claim", report)
        self.repository.finish(queued.id, claim, report)
        with self.assertRaises(Conflict):
            self.repository.finish(queued.id, claim, report)

    def test_expired_build_recovery_removes_worker_without_retrying_source(self):
        saved = self.service.save_source(self.owner, self.project, expected_head=None)
        queued = self.service.request_build(self.owner, self.shop, saved.revision)
        self.repository.claim(queued.id, -1)
        self.assertEqual(self.processor.recover_expired(), 1)
        self.assertEqual(self.builder.removed, queued.id)
        self.assertEqual(self.service.get_build(self.owner, self.shop, queued.id).state, "failed")
        self.assertEqual(self.processor.recover_expired(), 0)

    def test_expired_claim_cannot_publish_a_late_success(self):
        saved = self.service.save_source(self.owner, self.project, expected_head=None)
        queued = self.service.request_build(self.owner, self.shop, saved.revision)
        claim, source = self.repository.claim(queued.id, -1)
        report = self.builder.build(source)
        with self.assertRaises(Conflict):
            self.repository.finish(queued.id, claim, report)
        replacement = self.repository.fence_expired(queued.id)
        self.assertNotEqual(replacement, claim)
        with self.assertRaises(Conflict):
            self.repository.finish(queued.id, claim, report)

    def test_unconfirmed_build_cleanup_retains_capacity_until_recovery_without_replay(self):
        saved = self.service.save_source(self.owner, self.project, expected_head=None)
        first = self.service.request_build(self.owner, self.shop, saved.revision)
        second = self.service.request_build(self.owner, self.shop, saved.revision)

        def expire():
            with self.repository.transaction() as db:
                db.execute("UPDATE builds SET deadline='2000-01-01T00:00:00+00:00' WHERE id=?", (first.id,))

        with patch.object(self.builder, "build", wraps=self.builder.build) as build:
            with patch.object(self.builder, "remove", side_effect=SandboxError("cleanup unavailable")):
                with self.assertRaises(SandboxError):
                    self.processor.run_next()
                self.assertEqual(self.service.get_build(self.owner, self.shop, first.id).state, "running")
                self.assertIsNone(self.repository.claim_next(60))
                expire()
                with self.assertRaises(SandboxError):
                    self.processor.recover_expired()
                self.assertIsNone(self.repository.claim_next(60))
            expire()
            self.assertEqual(self.processor.recover_expired(), 1)
            self.assertEqual(build.call_count, 1, "recovery replayed source")
            self.assertEqual(self.service.get_build(self.owner, self.shop, first.id).state, "failed")
            self.assertTrue(self.processor.run_next())
            self.assertEqual(build.call_count, 2)
        self.assertEqual(self.service.get_build(self.owner, self.shop, second.id).state, "succeeded")

    def test_storage_corruption_is_detected_before_public_discovery(self):
        first = self.build()
        self.service.publish(self.owner, self.shop, first.id, expected_generation=0)
        with self.repository.transaction() as db:
            row = db.execute("SELECT report FROM builds WHERE id=?", (first.id,)).fetchone()
            db.execute(
                "UPDATE builds SET report=? WHERE id=?",
                (row["report"].replace(b"Thermal Processing Example", b"Forged Shop"), first.id),
            )
        with self.assertRaisesRegex(ValueError, "digest"):
            self.service.catalog(self.shop)


if __name__ == "__main__":
    unittest.main()
