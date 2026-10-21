"""A benchmark must not count malformed or refused responses as successful work."""

import copy
import importlib.util
import io
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    "deployment_load", Path(__file__).resolve().parents[1] / "scripts/deployment_load.py"
)
load = importlib.util.module_from_spec(spec)
spec.loader.exec_module(load)
fixture_spec = importlib.util.spec_from_file_location(
    "deployment_fixtures", Path(__file__).resolve().parents[1] / "scripts/deployment_fixtures.py"
)
fixtures = importlib.util.module_from_spec(fixture_spec)
fixture_spec.loader.exec_module(fixtures)


class DeploymentLoadTests(unittest.TestCase):
    def test_response_bound_allows_large_mcp_catalog_and_closes_on_overflow(self):
        value = {"data": "x" * (4 * 1024**2)}
        connection = Mock()
        response = Mock(status=200)
        body = io.BytesIO(json.dumps(value).encode())
        response.read.side_effect = body.read
        connection.getresponse.return_value = response
        self.assertEqual(load.Transport.send(connection, "GET", "/catalog"), (200, value))
        response.read.assert_called_once_with(load.MAX_RESPONSE_BYTES + 1)
        connection.close.assert_not_called()

        body = io.BytesIO(b"x" * (load.MAX_RESPONSE_BYTES + 1))
        response.read.side_effect = body.read
        with self.assertRaisesRegex(ValueError, "response exceeded limit"):
            load.Transport.send(connection, "GET", "/catalog")
        connection.close.assert_called_once()

    def qualification_report(self):
        return {
            "aggregate": {"requests": 100, "successes": 100, "unexpected": 0,
                          "missed_slots": 0, "successful_p95_ms": 50},
            "author": {"state": "succeeded", "finished_offset_seconds": 10},
            "author_completed_during_load": True, "noise": [], "competing_fixture": None,
        }

    def test_strict_qualification_checks_latency_as_well_as_complete_correct_work(self):
        report = self.qualification_report()
        def checks(value):
            return load.failed_checks(value, strict=True, max_p95_ms=100, max_author_seconds=30)
        self.assertEqual(checks(report), [])
        for key, value, expected in (
            ("successful_p95_ms", 100.01, "reader_latency_target"),
            ("successful_p95_ms", None, "reader_latency_target"),
            ("missed_slots", 1, "missed_reader_slots"),
            ("successes", 99, "refused_reader_requests"),
            ("unexpected", 1, "unexpected_reader_response"),
        ):
            changed = copy.deepcopy(report)
            changed["aggregate"][key] = value
            with self.subTest(key=key):
                self.assertIn(expected, checks(changed))
        report["author"]["finished_offset_seconds"] = 30.01
        self.assertEqual(checks(report), ["author_latency_target"])
        report["author"]["state"] = "failed"
        del report["author"]["finished_offset_seconds"]
        self.assertIn("author_failed", checks(report))

    def test_capacity_measurement_keeps_refusals_but_requires_actual_competing_work(self):
        report = self.qualification_report()
        report["aggregate"].update(successes=90, missed_slots=10, successful_p95_ms=150)
        report["author"].update(finished_offset_seconds=40)
        report["author_completed_during_load"] = False
        def checks():
            return load.failed_checks(report, strict=False, max_p95_ms=100, max_author_seconds=30)
        self.assertEqual(checks(), [])
        report["competing_fixture"] = {"shop": "large", "required_operations": ["rest:large", "mcp:large"]}
        report["noise"] = [{"successes": 0, "unexpected": 0, "successful_by_operation": {}}]
        self.assertEqual(checks(), ["competing_work_not_exercised"])
        report["noise"][0]["successes"] = 1
        report["noise"][0]["successful_by_operation"] = {"rest:large": 1}
        self.assertEqual(checks(), ["competing_work_not_exercised"])
        report["noise"][0]["successful_by_operation"]["mcp:large"] = 1
        self.assertEqual(checks(), [])
        report["noise"][0]["unexpected"] = 1
        self.assertEqual(checks(), ["unexpected_noise_response"])

    def test_larger_fixture_is_valid_ordinary_source_with_a_bounded_public_catalog(self):
        from chipload import BuildIdentity, compile_shop
        from chipload.discovery import public_catalog
        from chipload.project import run_cases

        project = fixtures.large_project()
        namespace = {}
        # This is the repository's trusted fixture; deployed builds use the worker.
        exec(compile(project.files[0].content, "load-fixture.py", "exec"), namespace)
        artifact = compile_shop(namespace["define_shop"](), BuildIdentity(
            source_sha256="a" * 64, sdk_sha256="b" * 64, dependencies_sha256="c" * 64,
            runtime="load-fixture-test",
        ))
        self.assertTrue(all(case.passed for case in run_cases(artifact, project.tests)))
        catalog = public_catalog(artifact)
        size = len(json.dumps(catalog).encode())
        self.assertGreater(size, 2_000_000)
        self.assertLess(size, 3 * 1024**2)
        self.assertEqual(len(catalog["catalogs"][0]["items"]), 500)
        self.assertEqual(len(artifact.shop.workflows[0].steps), 17)
        cases = fixtures.large_workload(project)
        self.assertEqual({case["action"] for case in cases}, {"catalog", "evaluate"})
        self.assertTrue(all(project.shop_id in case["path"] for case in cases))

    def test_valid_noise_uses_both_discovery_transports_and_validates_answers(self):
        config = {
            "start": 0, "seconds": 1, "rate": 4, "phase": 0,
            "role": "noise", "noise_profile": "large-shop", "shop": "large",
            "name": "Published", "revision": "a" * 64,
            "cases": [{"id": "catalog", "action": "catalog", "arguments": {},
                       "method": "GET", "path": "/v1/shops/large/catalog"}],
        }
        value = {"shop": {"id": "large", "name": "Published"}, "public_sha256": "a" * 64}
        clock, calls = [0], []

        class ProbeTransport:
            tokens = []

            def __init__(self, options):
                pass

            def call(self, method, path, **kwargs):
                calls.append(("rest", path))
                clock[0] += .01
                return 200, 200, value, None

            def tool(self, name, request):
                calls.append(("mcp", (name, request["shop_id"], request["action"])))
                clock[0] += .01
                return 200, 200, value, None

            def close(self):
                pass

        def sleep(seconds):
            clock[0] += seconds

        with (
            patch.object(load, "Transport", ProbeTransport),
            patch.object(load.time, "time", lambda: clock[0]),
            patch.object(load.time, "monotonic", lambda: clock[0]),
            patch.object(load.time, "perf_counter", lambda: clock[0]),
            patch.object(load.time, "sleep", sleep),
        ):
            result = load.reader(config)
        self.assertEqual(calls, [
            ("mcp", ("discover_shop", "large", "catalog")),
            ("rest", "/v1/shops/large/catalog"),
        ] * 2)
        self.assertFalse(result["authenticated"])
        self.assertEqual(result["missed_slots"], 0)
        self.assertEqual(load.summary(result["samples"], 1)["successes"], 4)

    def test_ingress_refusals_preserve_details_and_other_server_failures_stay_failures(self):
        status, _, code = load.decode(
            503,
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": 503,
                    "data": {"status": 503, "error": {"code": "request_capacity"}},
                },
            },
            mcp=True,
        )
        self.assertEqual((status, code), (503, "request_capacity"))
        self.assertTrue(load.backpressure(status, code))
        self.assertTrue(load.backpressure(429, "rate_limited"))
        for code in ("identity_unavailable", "service_interrupted", "http_error"):
            self.assertFalse(load.backpressure(503, code))

    def test_slow_or_late_clients_account_for_every_scheduled_slot(self):
        config = {
            "start": 0,
            "seconds": 2,
            "rate": 4,
            "phase": 0,
            "role": "reader",
            "shop": "example",
            "name": "Published",
            "revision": "a" * 64,
            "cases": [
                {
                    "id": "catalog",
                    "action": "catalog",
                    "arguments": {},
                    "method": "GET",
                    "path": "/catalog",
                }
            ],
        }
        value = {"shop": {"id": "example", "name": "Published"}, "public_sha256": "a" * 64}
        for initial, expected_calls in ((0, 4), (3, 0)):
            clock = [initial]

            class SlowTransport:
                tokens = []

                def __init__(self, config):
                    pass

                def call(self, *args, **kwargs):
                    clock[0] += 0.3
                    return 200, 200, value, None

                tool = call

                def close(self):
                    pass

            def sleep(seconds):
                clock[0] += seconds

            with (
                patch.object(load, "Transport", SlowTransport),
                patch.object(load.time, "time", lambda: clock[0]),
                patch.object(load.time, "monotonic", lambda: clock[0]),
                patch.object(load.time, "perf_counter", lambda: clock[0]),
                patch.object(load.time, "sleep", sleep),
            ):
                result = load.reader(config)
            self.assertEqual(len(result["samples"]), expected_calls)
            self.assertEqual(result["missed_slots"], 8 - expected_calls)
            self.assertFalse(any(s["unexpected"] for s in result["samples"]))

    def test_mcp_http_success_can_still_be_a_service_failure(self):
        status, value, code = load.decode(
            200,
            {
                "result": {
                    "isError": True,
                    "structuredContent": {
                        "status": 503,
                        "error": {"code": "data_busy"},
                    },
                }
            },
            mcp=True,
        )
        self.assertEqual((status, code), (503, "data_busy"))
        samples = [
            {
                "http": 200,
                "status": 200,
                "error": None,
                "unexpected": False,
                "operation": "rest:catalog",
                "ms": 50,
            },
            {
                "http": 200,
                "status": status,
                "error": code,
                "unexpected": False,
                "operation": "mcp:catalog",
                "ms": 1,
            },
            {
                "http": 200,
                "status": 200,
                "error": "ValueError",
                "unexpected": True,
                "operation": "rest:catalog",
                "ms": 1,
            },
        ]
        report = load.summary(samples, 10)
        self.assertEqual(report["successes"], 1)
        self.assertEqual(report["successful_by_operation"], {"rest:catalog": 1})
        self.assertEqual(report["successful_per_second"], 0.1)
        self.assertEqual(report["successful_p95_ms"], 50)
        self.assertEqual(report["unexpected"], 1)
        self.assertEqual(report["http_statuses"], {"200": 3})
        self.assertEqual(report["service_statuses"], {"200": 2, "503": 1})

    def test_discovery_checks_revision_visibility_and_business_results(self):
        config = {"shop": "example", "name": "Published", "revision": "a" * 64}
        case = {"action": "catalog"}
        value = {"shop": {"id": "example", "name": "Published"}, "public_sha256": "a" * 64}
        load.validate(value, case, config)
        for changed in (
            {**value, "public_sha256": "b" * 64},
            {**value, "shop": {"id": "example", "name": "Unpublished load draft"}},
            {**value, "private_details": {}},
            {**value, "description": "https://private-equipment.example.invalid"},
        ):
            with self.assertRaises(ValueError):
                load.validate(changed, case, config)
        case = {"action": "evaluate", "expected": "conditional", "reason": "missing-fact"}
        value = {
            "revision": "a" * 64,
            "outcome": "conditional",
            "simulated": True,
            "reasons": [{"code": "missing-fact"}],
        }
        load.validate(value, case, config)
        for changed in (
            {**value, "outcome": "supported"},
            {**value, "reasons": []},
            {**value, "simulated": False},
        ):
            with self.assertRaises(ValueError):
                load.validate(changed, case, config)

    def test_no_successes_has_no_success_latency_and_no_error_body(self):
        status, _, code = load.decode(
            401,
            {
                "error": {
                    "code": "secret-response-must-not-appear",
                    "detail": "private-value",
                }
            },
        )
        report = load.summary(
            [
                {
                    "http": 401,
                    "status": status,
                    "error": code,
                    "unexpected": True,
                    "operation": "catalog",
                    "ms": 20,
                }
            ],
            1,
        )
        self.assertEqual(report["successes"], 0)
        self.assertIsNone(report["successful_median_ms"])
        self.assertIsNone(report["successful_p95_ms"])
        self.assertNotIn("secret-response", json.dumps(report))
        self.assertNotIn("private-value", json.dumps(report))
        self.assertEqual(load.summary([], 1)["success_ratio"], 0)
