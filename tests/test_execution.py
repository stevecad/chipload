"""Execution authorization, durable intent, resource exclusion, and honest recovery."""

import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from execution_support import ExecutionFixture

from chipload.access import ACTIONS, Forbidden, Grant, Principal
from chipload.artifact import BuildIdentity, canonical_json, compile_shop, digest
from chipload.execution import validate_values
from chipload.execution_model import AdapterBinding
from chipload.executor import OperationRunner, Simulators, private_json
from chipload.lifecycle import Conflict, Repository
from chipload.model import Fact, Observation, ObservationRequirement, OperationField, Shop, quantity
from chipload.published import PublishedService
from chipload.quotas import QuotaExceeded, QuotaPolicy
from chipload.validation import ShopValidationError
from examples.callable_shop import define_shop, inputs


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.f = ExecutionFixture(Path(temporary.name) / "shop.db")

    def effects(self):
        with self.f.repository.connection() as db:
            return db.execute("SELECT COUNT(*) FROM simulation_effects").fetchone()[0]

    def test_exact_retries_share_one_effect_and_survive_reopening_storage(self):
        f = self.f
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = list(pool.map(lambda _: f.submit(), range(4)))
        self.assertEqual(len({job.id for job in jobs}), 1)
        self.assertTrue(f.runner.run_next())
        self.assertFalse(f.runner.run_next())
        completed = f.read(jobs[0].id)
        self.assertEqual(completed.state, "succeeded")
        self.assertEqual(completed.mode, "simulated")
        self.assertEqual(completed.result.outputs, (Fact(name="completed", value=True),))
        self.assertEqual(f.submit(), completed)
        self.assertEqual(self.effects(), 1)
        repository = Repository(f.repository.path)
        self.assertEqual(repository.read_quotas()["usage"]["deployment"]["retained_operations"], 1)
        with self.assertRaises(Conflict):
            f.submit(inputs=inputs("4"))

    def test_business_conditions_and_technical_inputs_block_dispatch(self):
        for facts in (inputs("11"), inputs("1.5"), inputs(engagement="production"), inputs()[:-1]):
            with self.subTest(facts=facts), self.assertRaises((ValueError, Conflict)):
                self.f.submit(inputs=facts)
        with self.assertRaises(ValueError):
            self.f.submit(inputs=(Fact(name="length", value=quantity("3", "kg")), *inputs()[1:]))
        with self.assertRaises(ValueError):
            self.f.submit(inputs=(*inputs(), Fact(name="unmodeled", value=True)))
        self.assertEqual(self.effects(), 0)

    def test_execution_requires_binding_and_explicit_matching_simulation_mode(self):
        with self.assertRaises(Conflict):
            self.f.submit(mode="live")
        with self.f.repository.transaction() as db:
            db.execute("DELETE FROM operation_bindings")
        with self.assertRaises(Conflict):
            self.f.submit()

    def test_shop_and_operation_permissions_are_checked_before_reading_records(self):
        f = self.f
        limited = Principal(
            subject="limited",
            grants=(Grant(shop_id=f.shop, actions=ACTIONS, operations=("inspect-sample",)),),
        )
        outsiders = [None, Principal(subject="outsider"), limited]
        outsiders.append(
            Principal(
                subject="split",
                grants=(
                    Grant(
                        shop_id=f.shop,
                        actions=("operation:execute",),
                        operations=("inspect-sample",),
                    ),
                    Grant(shop_id=f.shop, actions=("operation:read",), operations=("make-sample",)),
                ),
            )
        )
        for caller in outsiders:
            with self.subTest(caller=caller), self.assertRaises(Forbidden):
                f.execution.submit(caller, f.request())
            if caller is None or caller.subject != "split":
                with self.assertRaises(Forbidden):
                    f.execution.read(caller, f.shop, "make-sample", "0" * 64)
        with self.assertRaises(Forbidden):
            f.execution.submit(f.owner, f.request(shop_id="another-shop"))

    def test_queued_withdrawal_rebinding_and_revocation_prevent_effects(self):
        f = self.f
        first = f.submit()
        f.principals.clear()
        self.assertFalse(f.runner.run_next())
        self.assertEqual(f.read(first.id).result.code, "execution-grant-revoked")
        f.principals["owner"] = f.owner
        second = f.submit("second")
        binding = AdapterBinding(
            shop_id=f.shop,
            operation_id="make-sample",
            model_sha256=f.build.report.artifact.model_sha256,
            adapter_id="machine",
            mode="simulated",
        )
        f.execution.bind(binding, expected_generation=1)
        self.assertFalse(f.runner.run_next())
        self.assertEqual(f.read(second.id).result.code, "adapter-binding-changed")
        third = f.submit("third")
        f.service.withdraw(f.owner, f.shop, expected_generation=1)
        self.assertFalse(f.runner.run_next())
        self.assertEqual(f.read(third.id).result.code, "operation-publication-changed")
        self.assertEqual(self.effects(), 0)

    def test_cancel_before_dispatch_prevents_effect_and_running_cancel_is_only_a_request(self):
        f = self.f
        first = f.submit()
        cancelled = f.execution.cancel(f.owner, f.shop, "make-sample", first.id)
        self.assertEqual(cancelled.state, "cancelled")
        self.assertFalse(f.runner.run_next())
        second = f.submit("second")
        claimed = f.runner.claim_next()
        context = f.runner._context(claimed)
        pending = f.execution.cancel(f.owner, f.shop, "make-sample", second.id)
        self.assertEqual(pending.state, "running")
        self.assertTrue(pending.cancel_requested)
        f.runner.complete(context, f.adapters["machine"].execute(context))
        self.assertEqual(f.read(second.id).state, "cancelled")
        third = f.submit("third")
        context = f.runner._context(f.runner.claim_next())
        outcome = f.adapters["machine"].execute(context)
        f.execution.cancel(f.owner, f.shop, "make-sample", third.id)
        f.runner.complete(context, outcome)
        self.assertEqual(f.read(third.id).state, "succeeded")
        self.assertTrue(f.read(third.id).cancel_requested)

    def test_lost_process_after_effect_is_reconciled_without_reexecution(self):
        f = self.f
        job = f.submit()
        context = f.runner._context(f.runner.claim_next())
        outcome = f.adapters["machine"].execute(context)
        self.assertEqual(self.effects(), 1)

        f.at += timedelta(seconds=31)
        runner = OperationRunner(
            Repository(f.repository.path), f.adapters, f.principals.get, clock=lambda: f.at
        )
        self.assertTrue(runner.reconcile(job.id))
        self.assertEqual(f.read(job.id).state, "succeeded")
        with self.assertRaises(Conflict):
            f.runner.complete(context, outcome)
        self.assertFalse(runner.run_next())
        self.assertEqual(self.effects(), 1)

    def test_process_death_after_receipt_commit_preserves_one_effect(self):
        f = self.f
        job = f.submit()
        program = """
import os, sys
from datetime import datetime
from pathlib import Path
from chipload.access import ACTIONS, Grant, Principal
from chipload.executor import OperationRunner, SimulatedAdapter
from chipload.lifecycle import Repository
from examples.callable_shop import SIMULATORS
repository = Repository(Path(sys.argv[1]))
at = datetime.fromisoformat(sys.argv[2])
owner = Principal(subject="owner", grants=(Grant(shop_id="callable-example", actions=ACTIONS),))
adapter = SimulatedAdapter(repository, SIMULATORS["machine"], clock=lambda: at)
runner = OperationRunner(repository, {"machine": adapter}, {"owner": owner}.get, clock=lambda: at)
context = runner._context(runner.claim_next())
adapter.execute(context)
os._exit(73)
"""
        result = subprocess.run(
            [sys.executable, "-c", program, str(f.repository.path), f.at.isoformat()],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertEqual(f.read(job.id).state, "running")
        self.assertEqual(self.effects(), 1)
        f.at += timedelta(seconds=31)
        self.assertTrue(f.runner.reconcile(job.id))
        self.assertEqual(f.read(job.id).state, "succeeded")
        self.assertEqual(f.submit().id, job.id)
        self.assertFalse(f.runner.run_next())
        self.assertEqual(self.effects(), 1)

    def test_fencing_before_effect_prevents_a_late_simulated_dispatch(self):
        f = self.f
        job = f.submit()
        context = f.runner._context(f.runner.claim_next())
        f.at += timedelta(seconds=31)
        self.assertTrue(f.runner.reconcile(job.id))
        self.assertEqual(f.read(job.id).state, "rejected")
        self.assertEqual(f.adapters["machine"].execute(context).state, "rejected")
        self.assertEqual(self.effects(), 0)

    def test_unknown_outcome_holds_resources_and_never_retries_adapter_effect(self):
        f = self.f

        class Ambiguous:
            mode = "simulated"
            calls = 0

            def execute(self, context):
                self.calls += 1
                raise RuntimeError("sensitive adapter diagnostic")

            def reconcile(self, context):
                return None

        adapter = Ambiguous()
        f.adapters["machine"] = adapter
        first, second = f.submit(), f.submit("second")
        self.assertTrue(f.runner.run_next())
        uncertain = f.read(first.id)
        self.assertEqual(uncertain.state, "indeterminate")
        self.assertNotIn("sensitive", uncertain.model_dump_json())
        self.assertFalse(f.runner.reconcile(first.id))
        self.assertFalse(f.runner.run_next())
        self.assertEqual(f.read(second.id).state, "queued")
        self.assertEqual(adapter.calls, 1)

    def test_invalid_adapter_output_cannot_be_reported_as_success(self):
        f = self.f
        f.adapters["machine"].outputs = (Fact(name="completed", value="not a boolean"),)
        job = f.submit()
        self.assertTrue(f.runner.run_next())
        self.assertEqual(f.read(job.id).state, "indeterminate")
        self.assertEqual(self.effects(), 0)
        self.assertTrue(f.runner.reconcile(job.id))
        self.assertEqual(f.read(job.id).state, "rejected")

    def test_execution_capacity_is_atomic_and_exact_retries_do_not_spend_it_twice(self):
        f = self.f
        policy = f.repository.read_quotas()["policy"]
        policy["shop"]["pending_operations"] = 1
        f.repository.set_quotas(QuotaPolicy.model_validate(policy), expected_generation=0)
        first = f.submit()
        self.assertEqual(f.submit().id, first.id)
        with self.assertRaises(QuotaExceeded):
            f.submit("second")
        f.runner.run_next()
        second = f.submit("second")
        self.assertNotEqual(second.id, first.id)

    def test_empty_operation_extension_preserves_existing_artifact_identity(self):
        shop = Shop(id="empty", name="Empty")
        payload = shop.model_dump(mode="json")
        self.assertNotIn("operations", payload)
        identity = BuildIdentity(
            source_sha256="a" * 64,
            dependencies_sha256="b" * 64,
            sdk_sha256="c" * 64,
            runtime="fixture",
        )
        self.assertEqual(compile_shop(shop, identity).model_sha256, digest(canonical_json(payload)))

    def test_current_observations_are_rechecked_after_queue_admission(self):
        shop = define_shop()
        capability = shop.capabilities[0].model_copy(
            update={
                "observations": (
                    ObservationRequirement(
                        observation="ready",
                        resource="machine",
                        property="ready",
                        allowed=(True,),
                        purpose="readiness",
                        remedy="Check the machine.",
                    ),
                )
            }
        )
        f = ExecutionFixture(
            self.f.repository.path.parent / "observed.db",
            shop.model_copy(update={"capabilities": (capability, *shop.capabilities[1:])}),
        )
        with self.assertRaises(Conflict):
            f.submit()
        observations = PublishedService(f.service, clock=lambda: f.at)
        observations.write_observations(
            f.owner,
            f.shop,
            (
                Observation(
                    id="ready",
                    resource="machine",
                    property="ready",
                    value=True,
                    time=f.at,
                    max_age_seconds=10,
                    source="simulated",
                ),
            ),
            expected_revision=None,
        )
        job = f.submit()
        f.at += timedelta(seconds=11)
        self.assertFalse(f.runner.run_next())
        self.assertEqual(f.read(job.id).result.code, "operation-precondition-changed")

    def test_bounded_scans_reach_jobs_behind_unavailable_adapters(self):
        f = self.f
        policy = f.repository.read_quotas()["policy"]
        for scope in ("deployment", "shop", "identity"):
            policy[scope]["pending_operations"] = 150
        f.repository.set_quotas(QuotaPolicy.model_validate(policy), expected_generation=0)
        for index in range(101):
            f.submit(str(index))
        f.adapters.pop("machine")
        last = f.submit("inspection", operation_id="inspect-sample", inputs=inputs()[1:])
        self.assertFalse(f.runner.run_next())
        reopened = OperationRunner(
            Repository(f.repository.path), f.adapters, f.principals.get, clock=lambda: f.at
        )
        self.assertTrue(reopened.run_next())
        self.assertEqual(f.read(last.id, "inspect-sample").state, "succeeded")

    def test_reconciliation_reaches_receipts_after_a_batch_of_unavailable_adapters(self):
        f = self.f
        policy = f.repository.read_quotas()["policy"]
        for scope in ("deployment", "shop", "identity"):
            policy[scope]["pending_operations"] = 150
            policy[scope]["running_operations"] = 128
        f.repository.set_quotas(QuotaPolicy.model_validate(policy), expected_generation=0)
        for index in range(100):
            f.submit(str(index))
        # A recovery fixture with a batch of unresolved work owned by another adapter.
        with f.repository.transaction() as db:
            db.execute("UPDATE executions SET state='indeterminate',claim=NULL")
        f.adapters.pop("machine")
        last = f.submit("inspection", operation_id="inspect-sample", inputs=inputs()[1:])
        context = f.runner._context(f.runner.claim_next())
        f.adapters["inspection"].execute(context)
        f.at += timedelta(seconds=31)
        self.assertEqual(f.runner.reconcile_pending(), 0)
        reopened = OperationRunner(
            Repository(f.repository.path), f.adapters, f.principals.get, clock=lambda: f.at
        )
        self.assertEqual(reopened.reconcile_pending(), 1)
        self.assertEqual(f.read(last.id, "inspect-sample").state, "succeeded")

    def test_executor_configuration_is_private_bounded_and_unambiguous(self):
        path = self.f.repository.path.parent / "simulators.json"
        path.touch(mode=0o600)
        path.write_text('{"adapters":[]}')
        self.assertEqual(Simulators.model_validate(private_json(path)).adapters, ())
        alias = path.with_name("alias")
        alias.symlink_to(path)
        with self.assertRaises(OSError):
            private_json(alias)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            private_json(path)
        path.chmod(0o600)
        path.write_bytes(b" " * (1024 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "byte limit"):
            private_json(path)
        path.write_text('{"adapters":[],"adapters":[]}')
        with self.assertRaises(ValueError):
            private_json(path)
        with self.assertRaises(ValueError):
            Simulators.model_validate({"adapters": [{"id": "same", "outputs": []}] * 2})

    def test_resource_exclusion_is_atomic_and_indeterminate_work_keeps_capacity(self):
        f = self.f
        policy = f.repository.read_quotas()["policy"]
        for scope in ("deployment", "shop", "identity"):
            policy[scope]["running_operations"] = 2
        f.repository.set_quotas(QuotaPolicy.model_validate(policy), expected_generation=0)
        first, second = f.submit(), f.submit("second")
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(lambda _: f.runner.claim_next(), range(2)))
        self.assertEqual(sum(row is not None for row in claimed), 1)
        inspection = f.submit("inspection", operation_id="inspect-sample", inputs=inputs()[1:])
        self.assertTrue(f.runner.run_next())
        self.assertEqual(f.read(inspection.id, "inspect-sample").state, "succeeded")
        f.at += timedelta(seconds=31)
        f.runner.expire()
        self.assertEqual(f.read(first.id).state, "indeterminate")
        self.assertEqual(f.read(second.id).state, "queued")
        policy["deployment"]["running_operations"] = 1
        f.repository.set_quotas(QuotaPolicy.model_validate(policy), expected_generation=1)
        f.submit("inspection-2", operation_id="inspect-sample", inputs=inputs()[1:])
        self.assertFalse(f.runner.run_next())
        self.assertTrue(f.runner.reconcile(first.id))
        self.assertTrue(f.runner.run_next())

    def test_backup_requires_settled_operations_and_preserves_effect_identity(self):
        from recovery_support import archive

        from chipload.execution import ExecutionService

        f = self.f
        # Recovery uses fixed deployment filenames.
        original = ExecutionFixture(f.repository.path.parent / "data/chipload.db")
        job = original.submit()
        with self.assertRaisesRegex(ValueError, "operations"):
            archive.snapshot_pair(
                original.repository.path.parent, f.repository.path.parent / "busy"
            )
        original.runner.run_next()
        target = f.repository.path.parent / "restored"
        archive.snapshot_pair(original.repository.path.parent, target)
        restored = Repository(target / "chipload.db")
        result = ExecutionService(restored).submit(original.owner, original.request())
        self.assertEqual(result.id, job.id)
        self.assertEqual(result.state, "succeeded")
        with restored.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM simulation_effects").fetchone()[0], 1)

    def test_operation_definition_rejects_unbound_dependencies_and_duplicate_fields(self):
        shop = define_shop()
        identity = self.f.build.report.artifact.identity
        operation = shop.operations[0]
        for changed in (
            operation.model_copy(update={"resources": ()}),
            operation.model_copy(update={"inputs": operation.inputs * 2}),
        ):
            with self.assertRaises(ShopValidationError):
                compile_shop(shop.model_copy(update={"operations": (changed,)}), identity)
        for changes in (
            {"name": "dimension", "kind": "quantity"},
            {"name": "flag", "kind": "boolean", "unit": "mm"},
        ):
            with self.assertRaises(ValueError):
                OperationField(**changes)
        validate_values(
            (OperationField(name="distance", kind="quantity", unit="mm"),),
            (Fact(name="distance", value=quantity("1", "[in_i]")),),
        )
