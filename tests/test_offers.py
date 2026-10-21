"""Alternative routes preserve per-route evidence, state, limits and business rules."""

import unittest
from datetime import timedelta
from itertools import product

from pydantic import ValidationError

from chipload import (
    BuildIdentity,
    Capability,
    Choice,
    EvaluationRequest,
    Evidence,
    Fact,
    Offer,
    Range,
    Resource,
    Route,
    ShopValidationError,
    Step,
    Workflow,
    compile_shop,
    evaluate,
    quantity,
    read_artifact,
)
from chipload.artifact import canonical_json
from chipload.complexity import target_budgets
from chipload.discovery import public_catalog
from chipload.limits import MAX_RESULT_WEIGHT
from chipload.model import OperationCheck
from examples.alternative_routes import AT, PROFILES, cases, define_shop

IDENTITY = BuildIdentity(
    source_sha256="a" * 64, sdk_sha256="b" * 64, dependencies_sha256="c" * 64, runtime="offer-test"
)


class OfferTests(unittest.TestCase):
    def test_every_pair_of_route_outcomes_has_explicit_or_semantics(self):
        order = ("supported", "conditional", "unknown", "unsupported")
        for outcomes in product(order, repeat=2):
            with self.subTest(outcomes=outcomes):
                shop = define_shop("review")
                caps, proofs, resources = [], [], []
                for name, state in zip(("a", "b"), outcomes, strict=True):
                    caps.append(
                        Capability(
                            id=name,
                            name=name,
                            process="processing",
                            resources=(name,),
                            evidence=(name,) if state != "unknown" else (),
                        )
                    )
                    proofs.append(
                        Evidence(
                            id=name,
                            name=name,
                            level="declared",
                            source="simulated",
                            claim="Test scope",
                            recorded_at=AT,
                            capabilities=(name,),
                            resources=(name,),
                        )
                    )
                    resources.append(
                        Resource(
                            id=name,
                            name=name,
                            kind="test-resource",
                            status={"conditional": "inactive", "unsupported": "retired"}.get(
                                state, "active"
                            ),
                        )
                    )
                offer = Offer(
                    id="service",
                    name="Service",
                    alternatives=tuple(
                        Route(target_kind="capability", target=name) for name in ("a", "b")
                    ),
                )
                artifact = compile_shop(
                    shop.model_copy(
                        update={
                            "capabilities": tuple(caps),
                            "evidence": tuple(proofs),
                            "resources": tuple(resources),
                            "workflows": (),
                            "offers": (offer,),
                        }
                    ),
                    IDENTITY,
                )
                result = evaluate(
                    artifact,
                    EvaluationRequest(target_kind="offer", target="service", allow_simulated=True),
                    at=AT,
                )
                best = min(outcomes, key=order.index)
                self.assertEqual(result.outcome, best)
                self.assertEqual([a.decision.outcome for a in result.alternatives], list(outcomes))
                if best in {"supported", "conditional"}:
                    self.assertEqual(
                        result.matching_alternative.target, ("a", "b")[outcomes.index(best)]
                    )
                else:
                    self.assertIsNone(result.matching_alternative)

    def test_manufacturing_thermal_and_service_routes_cover_all_four_outcomes(self):
        for profile in PROFILES:
            artifact = compile_shop(define_shop(profile), IDENTITY)
            self.assertEqual(read_artifact(artifact.to_json()), artifact)
            for case in cases(profile):
                with self.subTest(profile=profile, case=case.id):
                    result = evaluate(artifact, case.request, at=AT)
                    self.assertEqual(result.outcome, case.expected)
                    self.assertEqual(len(result.alternatives), 2)
                    if case.expected in {"supported", "conditional"}:
                        expected = "contract-route" if case.id == "contract" else "small-lot-route"
                        self.assertEqual(result.matching_alternative.target, expected)
                    else:
                        self.assertIsNone(result.matching_alternative)

    def test_no_route_borrows_state_from_another_route(self):
        shop = define_shop()
        changed = tuple(
            cap.model_copy(update={"output_state": ()}) if cap.id == "contract" else cap
            for cap in shop.capabilities
        )
        artifact = compile_shop(shop.model_copy(update={"capabilities": changed}), IDENTITY)
        request = cases("machining")[1].request
        result = evaluate(artifact, request, at=AT)
        self.assertEqual(result.outcome, "unknown")
        self.assertIn("missing-input-state", {r.code for r in result.reasons})

    def test_shared_conditions_do_not_hide_stricter_route_requirements(self):
        shop = define_shop()
        offer = shop.offers[0].model_copy(
            update={
                "requirements": (
                    *shop.offers[0].requirements,
                    Range(fact="width", maximum=quantity("300", "mm")),
                )
            }
        )
        artifact = compile_shop(shop.model_copy(update={"offers": (offer,)}), IDENTITY)
        query = cases("machining")[0].request
        query = query.model_copy(
            update={
                "facts": tuple(
                    fact.model_copy(update={"value": quantity("100", "mm")})
                    if fact.name == "width"
                    else fact
                    for fact in query.facts
                )
            }
        )
        self.assertEqual(evaluate(artifact, query, at=AT).outcome, "unsupported")

    def test_shared_inputs_remain_available_to_route_evidence(self):
        shop = define_shop()
        proofs = tuple(
            proof.model_copy(
                update={
                    "requirements": (
                        *proof.requirements,
                        Choice(fact="terms-accepted", allowed=(True,)),
                    )
                }
            )
            if proof.id == "small-lot-declaration"
            else proof
            for proof in shop.evidence
        )
        artifact = compile_shop(shop.model_copy(update={"evidence": proofs}), IDENTITY)
        query = cases("machining")[0].request
        self.assertEqual(evaluate(artifact, query, at=AT).outcome, "supported")
        refused = query.model_copy(
            update={
                "facts": tuple(
                    fact.model_copy(update={"value": False})
                    if fact.name == "terms-accepted"
                    else fact
                    for fact in query.facts
                )
            }
        )
        self.assertEqual(evaluate(artifact, refused, at=AT).outcome, "unknown")

    def test_unknown_fields_remain_unknown_without_unbounded_route_diagnostics(self):
        cap = Capability(id="simple", name="Simple", process="processing", evidence=("proof",))
        proof = Evidence(
            id="proof",
            name="Proof",
            level="declared",
            source="simulated",
            claim="Test scope",
            recorded_at=AT,
            capabilities=(cap.id,),
        )
        workflows = tuple(
            Workflow(id=f"route{i}", name="Route", steps=(Step(id="s", capability=cap.id),))
            for i in range(32)
        )
        offer = Offer(
            id="service",
            name="Service",
            alternatives=tuple(Route(target_kind="workflow", target=w.id) for w in workflows),
        )
        shop = define_shop("review").model_copy(
            update={
                "capabilities": (cap,),
                "evidence": (proof,),
                "workflows": workflows,
                "offers": (offer,),
            }
        )
        artifact = compile_shop(shop, IDENTITY)
        query = EvaluationRequest(
            target_kind="offer",
            target=offer.id,
            allow_simulated=True,
            facts=tuple(Fact(name=f"field{i}", value="unused") for i in range(100)),
        )
        result = evaluate(artifact, query, at=AT)
        self.assertEqual(result.outcome, "unknown")
        self.assertIsNone(result.matching_alternative)
        self.assertLess(len(canonical_json(result.model_dump(mode="json"))), MAX_RESULT_WEIGHT)
        for route in result.alternatives:
            self.assertEqual(route.decision.outcome, "unknown")
            self.assertEqual(
                sum(r.code == "unmodeled-requirement" for r in route.decision.reasons), 1
            )

    def test_unknown_evidence_is_not_rejection_and_other_route_proof_is_not_reused(self):
        shop = define_shop()
        proofs = tuple(
            p.model_copy(update={"valid_until": AT + timedelta(seconds=1)})
            if p.id == "small-lot-declaration"
            else p
            for p in shop.evidence
        )
        artifact = compile_shop(shop.model_copy(update={"evidence": proofs}), IDENTITY)
        result = evaluate(artifact, cases("machining")[0].request, at=AT + timedelta(seconds=2))
        self.assertEqual(result.outcome, "unknown")
        self.assertNotIn("contract-declaration", {p.id for p in result.evidence})
        no_simulation = cases("machining")[0].request.model_copy(update={"allow_simulated": False})
        self.assertEqual(evaluate(artifact, no_simulation, at=AT).outcome, "unknown")

    def test_supported_route_beats_conditional_unknown_and_rejected_routes(self):
        shop = define_shop()
        source = shop.workflows[0]
        requirements = tuple(rule for rule in source.requirements if rule.fact != "engagement")
        conditional = source.model_copy(
            update={
                "id": "conditional",
                "requirements": (
                    *requirements,
                    Choice(
                        fact="engagement",
                        allowed=("reviewed",),
                        on_mismatch="conditional",
                        remedy="Review it.",
                    ),
                ),
            }
        )
        shared = shop.offers[0]
        offer = shared.model_copy(
            update={
                "alternatives": (
                    Route(target_kind="workflow", target="conditional"),
                    *shared.alternatives,
                )
            }
        )
        artifact = compile_shop(
            shop.model_copy(
                update={"workflows": (*shop.workflows, conditional), "offers": (offer,)}
            ),
            IDENTITY,
        )
        result = evaluate(artifact, cases("machining")[0].request, at=AT)
        self.assertEqual(result.outcome, "supported")
        self.assertEqual(result.matching_alternative.target, "small-lot-route")
        self.assertEqual(result.alternatives[0].decision.outcome, "conditional")

    def test_empty_unknown_duplicate_and_recursive_routes_are_rejected(self):
        for alternatives in ((), (Route(target_kind="workflow", target="x"),) * 2):
            with self.assertRaises(ValidationError):
                Offer(id="x", name="X", alternatives=alternatives)
        with self.assertRaises(ValidationError):
            Route(target_kind="offer", target="x")
        with self.assertRaises(ValidationError):
            OperationCheck(target_kind="offer", target="service")
        shop = define_shop()
        offer = Offer(
            id="x", name="X", alternatives=(Route(target_kind="capability", target="missing"),)
        )
        with self.assertRaises(ShopValidationError) as error:
            compile_shop(shop.model_copy(update={"offers": (offer,)}), IDENTITY)
        self.assertEqual(
            error.exception.diagnostics[0].path, ("offers", "x", "alternatives", "0", "target")
        )

    def test_combined_route_cost_cannot_bypass_work_or_output_limits(self):
        shop = define_shop()
        capability = Capability(
            id="expensive",
            name="Expensive",
            process="processing",
            requirements=tuple(Range(fact=f"f{i}", minimum=quantity("0", "mm")) for i in range(30)),
        )
        workflows = tuple(
            Workflow(id=f"route{i}", name="Route", steps=(Step(id="s", capability=capability.id),))
            for i in range(16)
        )
        base = shop.model_copy(
            update={
                "capabilities": (capability,),
                "workflows": workflows,
                "evidence": (),
                "offers": (),
            }
        )
        compile_shop(base, IDENTITY)
        offer = Offer(
            id="cost",
            name="Cost",
            alternatives=tuple(Route(target_kind="workflow", target=w.id) for w in workflows),
        )
        combined = base.model_copy(update={"offers": (offer,)})
        self.assertGreater(target_budgets(combined)["offer", "cost"][0], 100_000)
        with self.assertRaises(ShopValidationError) as error:
            compile_shop(combined, IDENTITY)
        self.assertIn(("offers", "cost"), {d.path for d in error.exception.diagnostics})

    def test_absent_offers_preserve_existing_artifact_and_public_identity(self):
        shop = define_shop().model_copy(update={"offers": ()})
        data = shop.model_dump(mode="json")
        self.assertNotIn("offers", data)
        artifact = compile_shop(shop, IDENTITY)
        catalog = public_catalog(artifact)
        private = Offer(
            id="private",
            name="Private",
            alternatives=(Route(target_kind="capability", target="small-lot"),),
        )
        altered = compile_shop(shop.model_copy(update={"offers": (private,)}), IDENTITY)
        self.assertEqual(canonical_json(catalog), canonical_json(public_catalog(altered)))


if __name__ == "__main__":
    unittest.main()
