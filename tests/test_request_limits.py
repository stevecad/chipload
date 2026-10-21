"""Shared request budgets under process contention, clock changes, and saturation."""

import multiprocessing
import pickle
import tempfile
import time
import unittest
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from unittest.mock import patch

from chipload.lifecycle import Repository
from chipload.quotas import QuotaPolicy
from chipload.request_limits import RateLimited, RequestCapacity, RequestLimiter, peer_key

AT = 1_792_454_400_000


def admit_process(arguments):
    path, peer = arguments
    limiter = RequestLimiter(Repository(Path(path)), clock=lambda: AT)
    until = time.monotonic() + 5
    while True:
        try:
            limiter.ingress((peer, 1234))
            return "accepted"
        except RateLimited:
            return "limited"
        except RequestCapacity:
            # A pre-admission refusal runs no application work. Wait for the
            # competing writer, while keeping the quota clock frozen at AT.
            if time.monotonic() >= until:
                raise
            time.sleep(.005)


def capacity_process():
    raise RequestCapacity()


class RequestLimitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "platform.db"
        self.repository = Repository(self.path)
        self.at = AT
        self.limiter = RequestLimiter(self.repository, clock=lambda: self.at)

    def policy(self, **changes):
        status = self.repository.read_quotas()
        for scope, values in changes.items():
            if isinstance(values, dict):
                status["policy"]["requests"][scope].update(values)
            else:
                status["policy"]["requests"][scope] = values
        self.repository.set_quotas(
            QuotaPolicy.model_validate(status["policy"]), expected_generation=status["generation"]
        )

    def peer(self, number):
        self.limiter.ingress((f"198.51.100.{number}", 1234))

    def test_processes_cannot_multiply_the_deployment_allowance(self):
        self.policy(deployment={"burst": 5})
        with ProcessPoolExecutor(
            max_workers=4, mp_context=multiprocessing.get_context("spawn")
        ) as pool:
            results = list(
                pool.map(admit_process, [(str(self.path), f"198.51.100.{i}") for i in range(12)])
            )
        self.assertEqual(results.count("accepted"), 5)
        self.assertEqual(results.count("limited"), 7)
        # Denied attempts leave no peer state behind.
        self.assertEqual(self.repository.read_quotas()["usage"]["deployment"]["request_buckets"], 6)

    def test_admission_refusals_preserve_their_type_and_metadata_across_processes(self):
        for original in (RequestCapacity(), RateLimited("identity", 7)):
            restored = pickle.loads(pickle.dumps(original))
            self.assertIs(type(restored), type(original))
            self.assertEqual(restored.retry_after, original.retry_after)
            self.assertEqual(str(restored), str(original))
            if isinstance(original, RateLimited):
                self.assertEqual(restored.limit, "identity")
        with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
            with self.assertRaises(RequestCapacity):
                pool.submit(capacity_process).result(timeout=10)

    def test_denied_peer_does_not_spend_another_peers_deployment_budget(self):
        self.policy(deployment={"burst": 2}, peer={"burst": 1})
        self.peer(1)
        with self.assertRaisesRegex(RateLimited, "request rate"):
            self.peer(1)
        self.peer(2)
        with self.assertRaises(RateLimited) as failure:
            self.peer(3)
        self.assertEqual(failure.exception.limit, "deployment")

    def test_reopening_repository_does_not_reset_a_bucket(self):
        self.policy(identity={"burst": 1})
        self.limiter.identity("owner")
        other = RequestLimiter(Repository(self.path), clock=lambda: AT)
        with self.assertRaises(RateLimited) as failure:
            other.identity("owner")
        self.assertEqual(failure.exception.limit, "identity")
        other.identity("another-owner")

    def test_fractional_refill_and_clock_rollback_do_not_grant_extra_requests(self):
        self.policy(identity={"per_minute": 7, "burst": 1})
        self.limiter.identity("owner")
        self.at += 8571
        with self.assertRaises(RateLimited) as failure:
            self.limiter.identity("owner")
        self.assertEqual(failure.exception.retry_after, 1)
        self.at += 1
        self.limiter.identity("owner")
        self.at -= 10_000
        with self.assertRaises(RateLimited) as failure:
            self.limiter.identity("owner")
        self.assertEqual(failure.exception.retry_after, 19)

    def test_active_buckets_are_not_evicted_to_admit_new_addresses(self):
        self.policy(
            max_buckets=8,
            peer={"per_minute": 1, "burst": 1},
            deployment={"per_minute": 60_000, "burst": 200},
        )
        for i in range(7):
            self.peer(i)
        with self.assertRaises(RequestCapacity):
            self.peer(8)
        with self.assertRaises(RateLimited):
            self.peer(0)
        self.assertEqual(self.repository.read_quotas()["usage"]["deployment"]["request_buckets"], 8)
        self.at += 60_000
        self.peer(8)
        self.assertEqual(self.repository.read_quotas()["usage"]["deployment"]["request_buckets"], 2)

    def test_policy_updates_preserve_spent_credit_and_apply_to_existing_processes(self):
        self.policy(identity={"per_minute": 1, "burst": 2})
        self.limiter.identity("owner")
        self.limiter.identity("owner")
        self.policy(identity={"burst": 10})
        with self.assertRaises(RateLimited):
            self.limiter.identity("owner")
        self.at += 60_000
        self.limiter.identity("owner")
        self.policy(identity={"burst": 1})
        with self.assertRaises(RateLimited):
            self.limiter.identity("owner")

    def test_mutations_share_identity_across_shops_and_shop_across_identities(self):
        self.policy(mutation_identity={"burst": 1}, mutation_shop={"burst": 1})
        self.limiter.mutation("owner", "shop-a")
        with self.assertRaises(RateLimited) as failure:
            self.limiter.mutation("owner", "shop-b")
        self.assertEqual(failure.exception.limit, "mutation_identity")
        with self.assertRaises(RateLimited) as failure:
            self.limiter.mutation("other", "shop-a")
        self.assertEqual(failure.exception.limit, "mutation_shop")
        self.limiter.mutation("other", "shop-b")

    def test_lower_rate_updates_refill_and_expiry_of_existing_buckets(self):
        self.policy(identity={"per_minute": 60, "burst": 1})
        self.limiter.identity("owner")
        self.policy(identity={"per_minute": 1})
        self.at += 1000
        self.peer(1)  # Reclaiming expired state must not use the old one-second expiry.
        with self.assertRaises(RateLimited) as failure:
            self.limiter.identity("owner")
        self.assertEqual(failure.exception.retry_after, 59)
        self.at += 59_000
        self.limiter.identity("owner")

    def test_database_contention_fails_closed_with_a_short_wait(self):
        with self.repository.admission.transaction():
            started = time.monotonic()
            with self.assertRaises(RequestCapacity):
                self.peer(1)
            self.assertLess(time.monotonic() - started, 1)
        self.peer(1)

    def test_exhausted_peer_is_refused_without_acquiring_the_writer(self):
        self.policy(peer={"per_minute": 1, "burst": 1})
        self.peer(1)
        with self.repository.admission.transaction():
            with self.assertRaises(RateLimited) as refused:
                self.peer(1)
            self.assertEqual(refused.exception.limit, "peer")
            # Read-only inspection can refuse, but cannot admit the fresh peer
            # while another transaction owns the accounting write lock.
            with self.assertRaises(RequestCapacity):
                self.peer(2)
        self.peer(2)

    def test_a_positive_read_snapshot_cannot_spend_credit_consumed_by_another_caller(self):
        self.policy(identity={"per_minute": 1, "burst": 1})
        other = RequestLimiter(self.repository, clock=lambda: self.at)
        charges = self.limiter._charges
        first = True

        def consumed(db, selected):
            nonlocal first
            result = charges(db, selected)
            if first:
                first = False
                other.identity("owner")
            return result

        with patch.object(self.limiter, "_charges", side_effect=consumed):
            with self.assertRaises(RateLimited):
                self.limiter.identity("owner")
        with self.assertRaises(RateLimited):
            other.identity("owner")

    def test_admission_rechecks_a_policy_edit_after_its_read_snapshot(self):
        self.policy(identity={"per_minute": 60, "burst": 1})
        self.limiter.identity("owner")
        self.at += 1000
        charges = self.limiter._charges
        first = True

        def reduced(db, selected):
            nonlocal first
            result = charges(db, selected)
            if first:
                first = False
                self.policy(identity={"per_minute": 1})
            return result

        with patch.object(self.limiter, "_charges", side_effect=reduced):
            with self.assertRaises(RateLimited) as refused:
                self.limiter.identity("owner")
        self.assertEqual(refused.exception.retry_after, 59)

    def test_a_previously_valid_policy_cannot_hide_corrupted_current_bytes(self):
        self.limiter.identity("owner")
        with self.repository.admission.transaction() as db:
            db.execute("UPDATE quota_policy SET policy=?", (b'{"requests":false}',))
        with self.assertRaises(RequestCapacity):
            self.limiter.identity("owner")

    def test_storage_failure_cannot_admit_requests(self):
        with self.repository.admission.transaction() as db:
            db.execute("DROP TABLE request_buckets")
        with self.assertRaises(RequestCapacity):
            self.peer(1)

    def test_shop_write_lock_does_not_block_requests_or_operator_policy_updates(self):
        with self.repository.transaction():
            self.peer(1)
            self.limiter.identity("owner")
            self.policy(peer={"burst": 2})
            reopened = Repository(self.path)
            RequestLimiter(reopened, clock=lambda: self.at).ingress(("198.51.100.2", 80))

    def test_peer_normalization_groups_ipv6_subnets_and_mapped_ipv4(self):
        self.assertEqual(peer_key(("2001:db8::1", 123)), peer_key(("2001:db8::999", 456)))
        self.assertNotEqual(peer_key(("2001:db8:1::1", 1)), peer_key(("2001:db8:2::1", 1)))
        self.assertEqual(peer_key(("::ffff:198.51.100.1", 1)), peer_key(("198.51.100.1", 2)))
        self.assertEqual(peer_key(None), "unknown")
        self.assertEqual(peer_key(("testclient", 50000)), "unknown")


if __name__ == "__main__":
    unittest.main()
