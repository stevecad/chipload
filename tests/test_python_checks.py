"""Python constraint composition, provenance binding, limits, and public privacy."""

import sys
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError
from server_support import IdentityFixture

from chipload import (
    BuildIdentity,
    Capability,
    EvaluationRequest,
    Offer,
    PythonCheck,
    Range,
    Route,
    ShopValidationError,
    Step,
    Workflow,
    compile_shop,
    evaluate,
    quantity,
)
from chipload.check_worker import run_checks
from chipload.discovery import public_catalog
from chipload.project import AcceptanceCase, BuildReport, run_cases
from chipload.python_checks import (
    CaseChecks,
    PythonCheckResult,
    bind_case_results,
    require_check_coverage,
    suite_plans,
    validate_check_sources,
)
from chipload.server import create_app
from examples import python_shop

AT = python_shop.AT


def built(profile="machining", source=None, shop=None):
    source = source or python_shop.project(profile)
    shop = shop or python_shop.define_shop(profile)
    artifact = compile_shop(
        shop,
        BuildIdentity(
            source_sha256=source.revision,
            sdk_sha256="a" * 64,
            dependencies_sha256="b" * 64,
            runtime="test",
        ),
    )
    with patch.dict(sys.modules, {"shop": python_shop}):
        answers = run_checks(shop, source)
    checks = bind_case_results(artifact, source.tests, answers)
    return source, artifact, checks


class PythonCheckTests(unittest.TestCase):
    def test_all_examples_use_real_functions_and_preserve_static_and_evidence_guards(self):
        for profile in python_shop.PROFILES:
            source, artifact, checks = built(profile)
            results = run_cases(artifact, source.tests, python_evaluations=checks)
            self.assertTrue(all(case.passed for case in results), str(results))
            self.assertEqual(len(results), 10)
            static = next(case for case in results if case.id == "static-limit")
            self.assertTrue(
                all(result.outcome == "supported" for result in static.python_evaluation.results)
            )
            self.assertEqual(static.actual, "unsupported")
            self.assertEqual(results[-1].actual, "unknown")
            self.assertEqual(evaluate(artifact, source.tests[0].request, at=AT).outcome, "unknown")
            self.assertEqual(
                evaluate(
                    artifact,
                    source.tests[0].request,
                    at=AT,
                    python_evaluation=checks[0],
                    revoked_evidence=frozenset({"declaration"}),
                ).outcome,
                "unknown",
            )

    def test_build_query_and_time_bindings_reject_stale_results(self):
        source, artifact, checks = built()
        query = source.tests[0].request
        for changes in (
            {"build_sha256": "0" * 64},
            {"request_sha256": "0" * 64},
            {"evaluated_at": AT + timedelta(seconds=1)},
        ):
            with self.assertRaises(ValueError):
                evaluate(
                    artifact, query, at=AT, python_evaluation=checks[0].model_copy(update=changes)
                )
        with self.assertRaises(ValueError):
            evaluate(artifact, source.tests[1].request, at=AT, python_evaluation=checks[0])

    def test_partial_duplicate_and_unrelated_check_results_are_rejected(self):
        source, artifact, checks = built()
        for results in (
            checks[0].results[:1],
            checks[0].results * 2,
            (
                *checks[0].results,
                PythonCheckResult(capability="other", check="other", outcome="supported"),
            ),
        ):
            with self.assertRaises(ValueError):
                evaluate(
                    artifact,
                    source.tests[0].request,
                    at=AT,
                    python_evaluation=checks[0].model_copy(update={"results": results}),
                )
        with self.assertRaises(ValidationError):
            PythonCheckResult(capability="service", check="relationship", outcome=True)
        malformed = [
            CaseChecks(id=case.id, results=check.results).model_dump(mode="json")
            for case, check in zip(source.tests, checks, strict=True)
        ]
        malformed[0]["id"] = "different"
        with self.assertRaises(ValueError):
            bind_case_results(artifact, source.tests, malformed)

    def test_conditional_requires_an_explicit_remedy(self):
        source, artifact, checks = built()
        cap = artifact.shop.capabilities[0]
        cap = cap.model_copy(
            update={
                "python_checks": tuple(
                    check.model_copy(update={"remedy": ""}) for check in cap.python_checks
                )
            }
        )
        shop = artifact.shop.model_copy(update={"capabilities": (cap,)})
        with patch.dict(sys.modules, {"shop": python_shop}), self.assertRaises(ValueError):
            run_checks(shop, source)

    def test_check_sources_inputs_and_coverage_are_validated(self):
        source, artifact, _ = built()
        cap = artifact.shop.capabilities[0]
        unknown = cap.python_checks[0].model_copy(update={"facts": ("not-declared",)})
        with self.assertRaises(ShopValidationError):
            compile_shop(
                artifact.shop.model_copy(
                    update={"capabilities": (cap.model_copy(update={"python_checks": (unknown,)}),)}
                ),
                artifact.identity,
            )
        missing = cap.python_checks[0].model_copy(update={"entrypoint": "absent:check"})
        altered = artifact.shop.model_copy(
            update={"capabilities": (cap.model_copy(update={"python_checks": (missing,)}),)}
        )
        with self.assertRaises(ValueError):
            validate_check_sources(altered, source)
        with self.assertRaises(ValueError):
            require_check_coverage(artifact.shop, ())

    def test_target_and_suite_check_counts_are_bounded(self):
        source, artifact, _ = built()
        checks = tuple(
            PythonCheck(id=f"check-{i}", name="Check", entrypoint="shop:check", facts=("value",))
            for i in range(8)
        )
        caps = tuple(
            Capability(
                id=f"cap-{i}",
                name="Capability",
                process="provide",
                requirements=(Range(fact="value", minimum=quantity("1", "1")),),
                python_checks=checks,
            )
            for i in range(17)
        )
        shop = artifact.shop.model_copy(
            update={
                "capabilities": caps,
                "evidence": (),
                "workflows": (
                    Workflow(
                        id="combined",
                        name="Combined",
                        steps=tuple(Step(id=cap.id, capability=cap.id) for cap in caps),
                    ),
                ),
            }
        )
        with self.assertRaises(ShopValidationError) as error:
            compile_shop(shop, artifact.identity)
        self.assertIn(("workflows", "combined"), {d.path for d in error.exception.diagnostics})
        shop = shop.model_copy(update={"workflows": ()})
        compile_shop(shop, artifact.identity)
        cases = tuple(
            AcceptanceCase(
                id=f"case-{i}",
                at=AT,
                expected="unknown",
                request=EvaluationRequest(target_kind="capability", target="cap-0"),
            )
            for i in range(100)
        )
        with self.assertRaises(ValueError):
            suite_plans(shop, cases)

    def test_checks_compose_through_offers_and_empty_fields_preserve_artifacts(self):
        source = python_shop.project("machining")
        shop = python_shop.define_shop()
        shop = shop.model_copy(
            update={
                "offers": (
                    Offer(
                        id="offer",
                        name="Offer",
                        alternatives=(Route(target_kind="workflow", target="service-route"),),
                    ),
                )
            }
        )
        cases = tuple(
            case.model_copy(
                update={
                    "request": case.request.model_copy(
                        update={"target_kind": "offer", "target": "offer"}
                    )
                }
            )
            for case in source.tests
        )
        source, artifact, checks = built(
            source=source.model_copy(update={"tests": cases}), shop=shop
        )
        self.assertTrue(
            all(
                case.passed for case in run_cases(artifact, source.tests, python_evaluations=checks)
            )
        )
        cap = shop.capabilities[0].model_copy(update={"python_checks": ()})
        self.assertNotIn("python_checks", cap.model_dump())

    def test_public_discovery_never_runs_source_or_exposes_entrypoints(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = IdentityFixture(directory)
            self.addCleanup(identity.client.close)
            operations = identity.operations()
            owner = identity.settings.bindings[0]
            source, artifact, checks = built()
            # Reuse the authorized fixture shop identity for this transport test.
            source = source.model_copy(update={"shop_id": "thermal-example"})
            artifact = compile_shop(
                artifact.shop.model_copy(update={"id": "thermal-example"}),
                artifact.identity.model_copy(update={"source_sha256": source.revision}),
            )
            checks = tuple(
                check.model_copy(update={"build_sha256": artifact.build_sha256}) for check in checks
            )
            previous = operations.service.get_source(owner, "thermal-example")
            operations.service.save_source(owner, source, expected_head=previous.revision)
            job = operations.service.request_build(owner, "thermal-example", source.revision)
            claim, _ = operations.service.repository.claim(job.id, 60)
            operations.service.repository.finish(
                job.id,
                claim,
                BuildReport(
                    source_revision=source.revision,
                    worker_image="sha256:" + "c" * 64,
                    status="succeeded",
                    artifact=artifact,
                    tests=run_cases(artifact, source.tests, python_evaluations=checks),
                ),
            )
            operations.service.publish(owner, "thermal-example", job.id, expected_generation=1)
            with (
                TestClient(
                    create_app(
                        identity.settings, operations=operations, verifier=identity.verifier
                    ),
                    base_url=identity.settings.base_url,
                ) as client,
                patch(
                    "chipload.check_worker.run_checks",
                    side_effect=AssertionError("source executed in API"),
                ),
            ):
                catalog = client.get("/v1/shops/thermal-example/catalog")
                self.assertNotIn("entrypoint", catalog.text)
                self.assertNotIn("shop:machining", catalog.text)
                response = client.post(
                    "/v1/shops/thermal-example/evaluate",
                    json={
                        "shop_id": "thermal-example",
                        "query": source.tests[0].request.model_dump(mode="json"),
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["outcome"], "unknown")
                self.assertIn(
                    "python-check-required", {r["code"] for r in response.json()["reasons"]}
                )
            hidden = artifact.shop.capabilities[0].model_copy(
                update={
                    "python_checks": tuple(
                        check.model_copy(update={"visibility": "private"})
                        for check in artifact.shop.capabilities[0].python_checks
                    )
                }
            )
            secret = compile_shop(
                artifact.shop.model_copy(update={"capabilities": (hidden,)}), artifact.identity
            )
            catalog = public_catalog(secret)
            self.assertFalse(catalog["capabilities"][0]["complete"])
            self.assertEqual(catalog["capabilities"][0]["python_checks"], [])
            self.assertFalse(catalog["workflows"][0]["complete"])


if __name__ == "__main__":
    unittest.main()
