"""Run the trusted reference project and its acceptance scenarios."""

import argparse
import json

from chipload import evaluate
from chipload.discovery import public_catalog

from .scenarios import AT, SCENARIOS
from .shop import build


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact", action="store_true", help="print the complete private artifact"
    )
    parser.add_argument("--public", action="store_true", help="print the public catalog projection")
    args = parser.parse_args()
    artifact = build()
    if args.artifact:
        print(artifact.to_json().decode())
        return
    if args.public:
        print(json.dumps(public_catalog(artifact), indent=2))
        return
    failures = []
    for scenario in SCENARIOS:
        result = evaluate(
            artifact, scenario.question, at=AT, observations=scenario.observation_snapshot
        )
        correct = result.outcome == scenario.expected and scenario.reason in {
            reason.code for reason in result.reasons
        }
        print(f"{'PASS' if correct else 'FAIL'} {scenario.name}: {result.outcome}")
        if not correct:
            failures.append(scenario.name)
            print(result.model_dump_json(indent=2))
    if failures:
        raise SystemExit(1)
    print(f"All {len(SCENARIOS)} scenarios passed; model {artifact.model_sha256}.")


if __name__ == "__main__":
    main()
