"""Live evidence revocation survives publications, retries, migration and recovery."""

import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from execution_support import CallableBuilder, ExecutionFixture
from server_support import IdentityFixture

from chipload import BuildIdentity, Evidence, compile_shop, evaluate
from chipload.access import Forbidden, Grant, Principal
from chipload.artifact import canonical_json, digest
from chipload.evidence import EvidenceRevocation, EvidenceService, RevokeEvidence, snapshot
from chipload.lifecycle import (
    BuildProcessor,
    Conflict,
    IntegrityError,
    NotFound,
    Repository,
    ShopService,
)
from chipload.published import PublishedService
from chipload.quotas import QuotaExceeded
from examples.alternative_routes import cases
from examples.alternative_routes import define_shop as routes
from examples.heat_treatment import AT, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import recovery_archive as archive  # noqa: E402


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.identity = IdentityFixture(self.root)
        self.addCleanup(self.identity.client.close)
        self.operations = self.identity.operations()
        self.service = self.operations.evidence
        self.published = self.operations.published
        self.repository = self.operations.service.repository
        self.owner = self.identity.settings.bindings[0]
        self.shop = "thermal-example"
        model = self.published.read_evidence(self.owner, self.shop)["revision"]
        self.revocation = RevokeEvidence(
            shop_id=self.shop,
            evidence_id="thermal-declaration",
            expected_model_sha256=model,
            reason="Inspection found the qualification invalid. Private report 42.",
        )

    def test_revocation_invalidates_cached_capability_and_workflow_without_public_details(self):
        public = self.published.catalog(self.shop)
        self.assertEqual(self.published.evaluation(self.shop, request())["outcome"], "supported")
        receipt = self.service.revoke(self.owner, self.revocation)
        self.assertEqual(receipt.revoked_at, AT)
        for query in (request(), request(target="treat-and-verify", workflow=True)):
            result = self.published.evaluation(self.shop, query)
            self.assertEqual(result["outcome"], "unknown")
            self.assertNotIn("thermal-declaration", str(result))
            self.assertNotIn("Private report", str(result))
            self.assertNotIn("revocation_revision", result)
        self.assertEqual(public, self.published.catalog(self.shop))
        private = self.published.evaluation(
            self.shop, request(), private=True, principal=self.owner
        )
        self.assertTrue(private["revocation_revision"])
        self.assertEqual(private["evidence"], [])
        evidence = self.published.read_evidence(self.owner, self.shop)
        self.assertEqual(
            evidence["revocations"],
            [receipt.model_dump(mode="json", include={"evidence_id", "revoked_at"})],
        )

    def test_concurrent_exact_retries_share_one_receipt_and_audit_event(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(lambda _: self.service.revoke(self.owner, self.revocation), range(4))
            )
        self.assertTrue(all(result == results[0] for result in results))
        events = self.operations.service.audit(self.owner, self.shop)
        self.assertEqual(sum(event["action"] == "evidence.revoked" for event in events), 1)
        with self.assertRaises(Conflict):
            self.service.revoke(
                self.owner, self.revocation.model_copy(update={"reason": "Changed"})
            )
        self.operations.service.withdraw(self.owner, self.shop, expected_generation=1)
        self.assertEqual(self.service.revoke(self.owner, self.revocation), results[0])
        self.assertEqual(self.service.read(self.owner, self.revocation), results[0])

    def test_rollback_and_redefined_proof_cannot_restore_a_revoked_identity(self):
        service = self.operations.service
        original = service.get_publication(self.owner, self.shop).build_id
        self.service.revoke(self.owner, self.revocation)
        artifact, _, _ = self.published._snapshot(self.shop)
        changed = artifact.shop.model_copy(
            update={
                "evidence": tuple(
                    proof.model_copy(update={"claim": "A revised declaration"})
                    for proof in artifact.shop.evidence
                ),
            }
        )
        source = service.get_source(self.owner, self.shop)
        job = service.request_build(self.owner, self.shop, source.revision)
        BuildProcessor(self.repository, CallableBuilder(changed)).run(job.id)
        service.publish(self.owner, self.shop, job.id, expected_generation=1)
        self.assertEqual(self.published.evaluation(self.shop, request())["outcome"], "unknown")
        service.rollback(self.owner, self.shop, original, expected_generation=2)
        self.assertEqual(self.published.evaluation(self.shop, request())["outcome"], "unknown")
        service.withdraw(self.owner, self.shop, expected_generation=3)
        service.publish(self.owner, self.shop, original, expected_generation=4)
        reopened = PublishedService(ShopService(Repository(self.repository.path)), clock=lambda: AT)
        self.assertEqual(reopened.evaluation(self.shop, request())["outcome"], "unknown")

    def test_permissions_model_guard_and_missing_records_precede_mutation(self):
        for principal in (
            None,
            Principal(subject="outsider"),
            Principal(
                subject="reader",
                grants=(Grant(shop_id=self.shop, actions=("evidence:read", "publication:write")),),
            ),
        ):
            with self.assertRaises(Forbidden):
                self.service.revoke(principal, self.revocation)
        other = self.revocation.model_copy(update={"shop_id": "other"})
        with self.assertRaises(Forbidden):
            self.service.revoke(self.owner, other)
        with self.assertRaises(Forbidden):
            self.service.read(None, self.revocation)
        with self.assertRaises(NotFound):
            self.service.read(self.owner, self.revocation)
        with self.assertRaises(Conflict):
            self.service.revoke(
                self.owner, self.revocation.model_copy(update={"expected_model_sha256": "0" * 64})
            )
        with self.assertRaises(NotFound):
            self.service.revoke(
                self.owner, self.revocation.model_copy(update={"evidence_id": "missing"})
            )
        self.assertEqual(self.published.evaluation(self.shop, request())["outcome"], "supported")

    def test_publication_and_revocation_are_read_in_one_snapshot(self):
        original = self.repository.build_record
        done = False

        def read(row):
            nonlocal done
            result = original(row)
            if not done:
                done = True
                self.service.revoke(self.owner, self.revocation)
            return result

        with patch.object(self.repository, "build_record", side_effect=read):
            self.assertEqual(
                self.published.evaluation(self.shop, request())["outcome"], "supported"
            )
        self.assertEqual(self.published.evaluation(self.shop, request())["outcome"], "unknown")

    def test_quota_rejects_new_records_but_keeps_exact_retry_available(self):
        self.service.revoke(self.owner, self.revocation)
        with patch("chipload.evidence.MAX_REVOCATIONS_PER_SHOP", 1):
            self.service.revoke(self.owner, self.revocation)
            with self.assertRaises(QuotaExceeded):
                self.service.revoke(
                    self.owner,
                    self.revocation.model_copy(update={"evidence_id": "cooling-declaration"}),
                )

    def test_audit_failure_rolls_back_the_revocation_and_can_be_retried(self):
        original = self.repository.event

        def interrupted(*args):
            original(*args)
            raise RuntimeError("interrupted before commit")

        with patch.object(self.repository, "event", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.service.revoke(self.owner, self.revocation)
        with self.assertRaises(NotFound):
            self.service.read(self.owner, self.revocation)
        self.assertEqual(self.published.evaluation(self.shop, request())["outcome"], "supported")
        self.service.revoke(self.owner, self.revocation)
        events = self.operations.service.audit(self.owner, self.shop)
        self.assertEqual(sum(event["action"] == "evidence.revoked" for event in events), 1)

    def test_maximum_snapshot_is_bounded_to_this_models_evidence_and_shop(self):
        artifact, _, _ = self.published._snapshot(self.shop)
        proofs = (
            *artifact.shop.evidence,
            *(
                Evidence(
                    id=f"proof-{i:04}",
                    name="Proof",
                    level="declared",
                    source="simulated",
                    recorded_at=AT,
                    claim="Test scope",
                    capabilities=("thermal-cycle",),
                )
                for i in range(1000 - len(artifact.shop.evidence))
            ),
        )
        artifact = compile_shop(
            artifact.shop.model_copy(update={"evidence": proofs}), artifact.identity
        )
        with self.repository.transaction() as db:
            db.execute("INSERT INTO shops(id) VALUES('other')")
            for shop_id, proof_id in [
                *((self.shop, proof.id) for proof in proofs),
                (self.shop, "historical-proof"),
                ("other", proofs[0].id),
            ]:
                record = EvidenceRevocation(
                    shop_id=shop_id,
                    evidence_id=proof_id,
                    model_sha256=artifact.model_sha256,
                    reason="x" * 1000,
                    actor="owner",
                    revoked_at=AT,
                )
                payload = canonical_json(record)
                db.execute(
                    "INSERT INTO evidence_revocations VALUES(?,?,?,?)",
                    (shop_id, proof_id, payload, digest(payload)),
                )
            db.execute(
                "UPDATE evidence_revocations SET record=? WHERE evidence_id='historical-proof' OR shop_id='other'",
                (b"{}",),
            )
        with self.repository.connection() as db:
            result = snapshot(db, artifact)
        self.assertEqual(len(result.records), 1000)
        self.assertEqual(result.ids, frozenset(proof.id for proof in proofs))
        self.assertEqual([record.evidence_id for record in result.records], sorted(result.ids))
        self.assertTrue(all(record.shop_id == self.shop for record in result.records))

    def test_corrupt_receipts_fail_closed_and_cannot_be_overwritten(self):
        self.service.revoke(self.owner, self.revocation)
        with self.repository.transaction() as db:
            db.execute("UPDATE evidence_revocations SET record=?", (b"{}",))
        for call in (
            lambda: self.published.evaluation(self.shop, request()),
            lambda: self.service.read(self.owner, self.revocation),
            lambda: self.service.revoke(self.owner, self.revocation),
        ):
            with self.assertRaises(IntegrityError):
                call()

    def test_schema_migration_is_atomic_and_safe_for_concurrent_startup(self):
        with self.repository.transaction() as db:
            db.execute("DROP TABLE evidence_revocations")
            db.execute("PRAGMA user_version=9")
        with patch("chipload.lifecycle.EVIDENCE_SCHEMA", "CREATE TABLE partial(id); INVALID SQL"):
            with self.assertRaises(sqlite3.OperationalError):
                Repository(self.repository.path)
        with self.repository.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertIsNone(
                db.execute("SELECT name FROM sqlite_master WHERE name='partial'").fetchone()
            )
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: Repository(self.repository.path), range(4)))
        with self.repository.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 10)
        self.service.revoke(self.owner, self.revocation)

    def test_recovery_preserves_revocations_and_their_effect_on_queries(self):
        # snapshot_pair uses the production paired-store recovery path.
        receipt = self.service.revoke(self.owner, self.revocation)
        data = self.root / "recovery-source"
        data.mkdir(mode=0o700)
        # The fixture has a different database basename; use SQLite backup to
        # construct the same named pair used by deployed recovery.
        from contextlib import closing

        for source, name in (
            (self.repository.path, "chipload.db"),
            (self.repository.admission.path, "chipload.db.admission"),
        ):
            with (
                closing(sqlite3.connect(source)) as src,
                closing(sqlite3.connect(data / name)) as dst,
            ):
                src.backup(dst)
            (data / name).chmod(0o600)
        destination = self.root / "restored"
        archive.snapshot_pair(data, destination)
        repository = Repository(destination / "chipload.db")
        restored = EvidenceService(repository, clock=lambda: AT)
        self.assertEqual(restored.read(self.owner, self.revocation), receipt)
        published = PublishedService(ShopService(repository), clock=lambda: AT)
        self.assertEqual(published.evaluation(self.shop, request())["outcome"], "unknown")

    def test_operation_admission_and_dispatch_both_recheck_revocation(self):
        fixture = ExecutionFixture(self.root / "operations/shop.db")
        evidence = EvidenceService(fixture.repository, clock=lambda: fixture.at)
        queued = fixture.submit()
        evidence.revoke(
            fixture.owner,
            RevokeEvidence(
                shop_id=fixture.shop,
                evidence_id="machine-proof",
                expected_model_sha256=fixture.build.report.artifact.model_sha256,
                reason="Qualification withdrawn",
            ),
        )
        with self.assertRaises(Conflict):
            fixture.submit("new")
        self.assertFalse(fixture.runner.run_next())
        result = fixture.read(queued.id)
        self.assertEqual(result.state, "rejected")
        self.assertEqual(result.result.code, "operation-precondition-changed")
        with fixture.repository.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM simulation_effects").fetchone()[0], 0)

    def test_alternative_routes_and_remaining_valid_proofs_stay_independent(self):
        shop = routes()
        proof = shop.evidence[0].model_copy(update={"id": "replacement"})
        cap = shop.capabilities[0].model_copy(
            update={"evidence": (*shop.capabilities[0].evidence, proof.id)}
        )
        shop = shop.model_copy(
            update={
                "evidence": (*shop.evidence, proof),
                "capabilities": (cap, *shop.capabilities[1:]),
            }
        )
        artifact = compile_shop(
            shop,
            BuildIdentity(
                source_sha256="a" * 64,
                sdk_sha256="b" * 64,
                dependencies_sha256="c" * 64,
                runtime="test",
            ),
        )
        query = cases("machining")[0].request
        revoked = frozenset({"small-lot-declaration"})
        self.assertEqual(
            evaluate(artifact, query, at=AT, revoked_evidence=revoked).outcome, "supported"
        )
        revoked |= {"replacement"}
        self.assertEqual(
            evaluate(artifact, query, at=AT, revoked_evidence=revoked).outcome, "unknown"
        )
        self.assertEqual(
            evaluate(
                artifact, cases("machining")[1].request, at=AT, revoked_evidence=revoked
            ).outcome,
            "supported",
        )


if __name__ == "__main__":
    unittest.main()
