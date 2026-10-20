"""Adversarial data shapes and repeated graph work at trusted boundaries."""

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from pydantic import ValidationError

from chipload import (
    BuildIdentity,
    Capability,
    Choice,
    Evidence,
    Process,
    Range,
    Resource,
    Shop,
    ShopValidationError,
    ShopVariable,
    Step,
    Workflow,
    compile_shop,
    quantity,
    read_artifact,
)
from chipload.evaluate import EvaluationRequest
from chipload.graph import DependencyCycle, ordered_nodes
from chipload.limits import MAX_JSON_DEPTH, MAX_JSON_TOKENS, bounded_json
from chipload.model import Fact
from chipload.project import AcceptanceCase, run_cases
from chipload.validation import validate_shop

IDENTITY = BuildIdentity(
    source_sha256="a" * 64,
    dependencies_sha256="b" * 64,
    sdk_sha256="c" * 64,
    runtime="data-limits-test",
)


def shop_with(capability, steps=()):
    return Shop(
        id="example",
        name="Example",
        processes=(Process(id="process", name="Process"),),
        capabilities=(capability,),
        workflows=(Workflow(id="route", name="Route", steps=steps),) if steps else (),
    )


def expensive_capability(count=64):
    return Capability(
        id="offer",
        name="Offer",
        process="process",
        requirements=tuple(Range(fact=f"f-{i}", minimum=quantity("0", "mm")) for i in range(count)),
    )


class JSONLimitsTests(unittest.TestCase):
    def test_depth_and_structural_limits_precede_decoder_allocation(self):
        for payload in (
            b"[" * (MAX_JSON_DEPTH + 1) + b"0" + b"]" * (MAX_JSON_DEPTH + 1),
            b"[" + b"0," * MAX_JSON_TOKENS + b"0]",
            b" " * (8 * 1024 * 1024 + 1),
            b'"' + b"\\\\" * MAX_JSON_TOKENS + b'"',
        ):
            with self.subTest(bytes=len(payload)), patch("chipload.limits.json.loads") as decode:
                with self.assertRaises(ValueError):
                    bounded_json(payload)
                decode.assert_not_called()
        accepted = b"[" * MAX_JSON_DEPTH + b"0" + b"]" * MAX_JSON_DEPTH
        self.assertEqual(bounded_json(accepted), json.loads(accepted))

    def test_escaped_strings_cannot_change_preflight_nesting(self):
        samples = [
            "",
            '"',
            "\\",
            '\\"',
            "\\\\",
            '\\\\\\"',
            "[{}]:,",
            'with "quoted" \\ text and \n a newline',
            "é 😀",
            "{}[],:" * 20_000,
        ]
        for value in samples:
            payload = {"source": value, "after": ["valid"]}
            for ascii_only in (True, False):
                with self.subTest(value=value[:40], ascii_only=ascii_only):
                    encoded = json.dumps(payload, ensure_ascii=ascii_only).encode()
                    self.assertEqual(bounded_json(encoded), payload)
        # An escaped quote must not conceal actual containers after the string.
        attack = b'["\\\\",' + b"[" * 33 + b"0" + b"]" * 33 + b"]"
        with self.assertRaisesRegex(ValueError, "nesting"):
            bounded_json(attack)

    def test_nonfinite_long_numbers_duplicate_keys_and_invalid_utf8_fail(self):
        for payload in (
            b"NaN",
            b"Infinity",
            b"-Infinity",
            b"1e9999",
            b"9" * 101,
            b'{"x":1,"\\u0078":2}',
            b'{"a":{"x":1,"x":2}}',
            b'"\xff"',
            b"[}",
            b'"unfinished\\',
            b"[true false]",
        ):
            with self.subTest(payload=payload[:40]), self.assertRaises(ValueError):
                bounded_json(payload)
        self.assertEqual(bounded_json(b"[12,-0.25,1e2]"), [12, -0.25, 100.0])

    def test_artifact_reader_uses_same_preflight(self):
        with self.assertRaisesRegex(ValueError, "nesting"):
            read_artifact(b"[" * 33 + b"0" + b"]" * 33)


class ModelBudgetTests(unittest.TestCase):
    def test_oversized_array_stops_before_nested_validation(self):
        with self.assertRaises(ValidationError) as failed:
            Shop.model_validate({"id": "example", "name": "Example", "resources": [None] * 1001})
        self.assertEqual(failed.exception.error_count(), 1)
        self.assertIn("resources exceeds", str(failed.exception))
        for kwargs in (
            {"resources": tuple("r" for _ in range(257))},
            {"evidence": tuple("e" for _ in range(33))},
        ):
            with self.assertRaises(ValidationError):
                Capability(id="offer", name="Offer", process="process", **kwargs)
        with self.assertRaises(ValidationError):
            Fact(name="value", value="x" * 4001)
        with self.assertRaises(ValidationError):
            Choice(fact="value", allowed=tuple(f"v-{i}" for i in range(257)))
        schema = Shop.model_json_schema()
        self.assertEqual(schema["properties"]["resources"]["maxItems"], 1000)
        self.assertEqual(schema["$defs"]["Capability"]["properties"]["evidence"]["maxItems"], 32)

    def test_diagnostics_are_bounded_for_many_bad_references(self):
        shop = Shop(
            id="example",
            name="Example",
            resources=tuple(
                Resource(id=f"r-{i}", name="Resource", kind="custom", parent="missing")
                for i in range(300)
            ),
        )
        with self.assertRaises(ShopValidationError) as failed:
            validate_shop(shop)
        self.assertEqual(len(failed.exception.diagnostics), 100)
        self.assertTrue(all(d.code == "missing-reference" for d in failed.exception.diagnostics))

    def test_repeated_capability_steps_count_repeatedly(self):
        cap = expensive_capability()
        # Each offer is admissible in isolation.
        compile_shop(shop_with(cap), IDENTITY)
        steps = tuple(Step(id=f"s-{i}", capability=cap.id) for i in range(7))
        with self.assertRaises(ShopValidationError) as failed:
            compile_shop(shop_with(cap, steps), IDENTITY)
        self.assertTrue(
            any(
                d.code == "model-complexity"
                and "work score" in d.message
                and d.path == ("workflows", "route")
                for d in failed.exception.diagnostics
            )
        )

    def test_large_result_and_state_are_separate_budgets(self):
        cap = Capability(
            id="offer",
            name="Offer",
            process="process",
            requirements=tuple(
                Choice(fact=f"f-{i}", allowed=(True,), remedy="x" * 4000) for i in range(70)
            ),
        )
        with self.assertRaisesRegex(ShopValidationError, "result weight"):
            compile_shop(shop_with(cap), IDENTITY)
        caps = tuple(
            Capability(
                id=f"c-{i}",
                name="Offer",
                process="process",
                output_state=tuple(Fact(name=f"f-{i}-{j}", value=True) for j in range(100)),
            )
            for i in range(5)
        )
        shop = shop_with(caps[0]).model_copy(
            update={
                "capabilities": caps,
                "workflows": (
                    Workflow(
                        id="route",
                        name="Route",
                        steps=tuple(
                            Step(id=f"s-{i}", capability=cap.id) for i, cap in enumerate(caps)
                        ),
                    ),
                ),
            }
        )
        with self.assertRaisesRegex(ShopValidationError, "state exceeds"):
            compile_shop(shop, IDENTITY)

    def test_evidence_and_all_variable_predicates_consume_work(self):
        cap = expensive_capability(20)
        proofs = tuple(
            Evidence(
                id=f"proof-{i}", name="Proof", level="declared", source="simulated",
                claim="Test only", capabilities=(cap.id,), requirements=cap.requirements,
                recorded_at=datetime(2026, 10, 20, tzinfo=timezone.utc),
            ) for i in range(32)
        )
        cap = cap.model_copy(update={"evidence": tuple(p.id for p in proofs)})
        shop = shop_with(cap).model_copy(update={"evidence": proofs})
        with self.assertRaisesRegex(ShopValidationError, "work score"):
            compile_shop(shop, IDENTITY)
        variables = tuple(
            ShopVariable(
                id=f"v-{i}", name="Variable", category="custom", value=True,
                certainty="declared", source="simulated",
                recorded_at=datetime(2026, 10, 20, tzinfo=timezone.utc),
                applies_when=expensive_capability(4).requirements,
            ) for i in range(100)
        )
        with self.assertRaisesRegex(ShopValidationError, "variable applicability"):
            compile_shop(Shop(id="example", name="Example", variables=variables), IDENTITY)

    def test_wide_state_joins_count_value_comparisons(self):
        cap = Capability(id="offer", name="Offer", process="process")
        roots = tuple(Step(id=f"root-{i}", capability=cap.id) for i in range(20))
        join = Step(id="join", capability=cap.id, after=tuple(s.id for s in roots))
        with self.assertRaisesRegex(ShopValidationError, "work score"):
            compile_shop(shop_with(cap, (*roots, join)), IDENTITY)

    def test_acceptance_suite_cannot_multiply_work_without_a_limit(self):
        artifact = compile_shop(shop_with(expensive_capability(40)), IDENTITY)
        cases = tuple(
            AcceptanceCase(
                id=f"case-{i}",
                at=datetime.now(timezone.utc),
                expected="unknown",
                request=EvaluationRequest(target_kind="capability", target="offer"),
            )
            for i in range(100)
        )
        with patch("chipload.project.evaluate") as evaluator:
            with self.assertRaisesRegex(ValueError, "Acceptance suite exceeds"):
                run_cases(artifact, cases)
            evaluator.assert_not_called()

    def test_unchecked_model_copy_cannot_bypass_limits_at_compilation(self):
        cap = expensive_capability(1)
        invalid = cap.model_copy(update={"requirements": cap.requirements * 101})
        shop = shop_with(cap).model_copy(update={"capabilities": (invalid,)})
        with self.assertRaises(ValidationError):
            compile_shop(shop, IDENTITY)


class GraphTests(unittest.TestCase):
    def test_layers_are_stable_and_cycles_do_not_recurse(self):
        graph = {"z": ("a",), "a": (), "b": (), "c": ("b",), "d": ("c", "z")}
        self.assertEqual(ordered_nodes(graph), ("a", "b", "c", "z", "d"))
        self.assertEqual(ordered_nodes({"a": ("missing",)}), ("a",))
        with self.assertRaises(DependencyCycle) as failed:
            ordered_nodes({"a": ("b",), "b": ("a",), "c": ("a",)})
        self.assertEqual(failed.exception.nodes, ("a", "b", "c"))

    def test_long_chain_visits_each_dependency_once(self):
        visited = []

        class Parents(tuple):
            def __iter__(self):
                for parent in super().__iter__():
                    visited.append(parent)
                    yield parent

        graph = {f"n-{i}": Parents((f"n-{i - 1}",) if i else ()) for i in range(10_000)}
        ordered = ordered_nodes(graph)
        self.assertEqual(ordered, tuple(f"n-{i}" for i in range(10_000)))
        self.assertEqual(len(visited), 9999)


if __name__ == "__main__":
    unittest.main()
