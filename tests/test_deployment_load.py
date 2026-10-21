"""A benchmark must not count malformed or refused responses as successful work."""

import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "deployment_load", Path(__file__).resolve().parents[1] / "scripts/deployment_load.py"
)
load = importlib.util.module_from_spec(spec)
spec.loader.exec_module(load)


class DeploymentLoadTests(unittest.TestCase):
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
