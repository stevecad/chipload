"""Run, inspect, or export the fictional technical/business example matrix."""

import argparse
import json
import sys
from pathlib import Path

from chipload.discovery import public_catalog
from chipload.project import run_cases
from examples.common import build_example

from . import mixed
from .export import project
from .library import BY_ID
from .scenarios import scenarios
from .shops import define_shop


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("example", nargs="?", default="all", choices=("all", "mixed", *BY_ID))
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--list", action="store_true", help="list all examples and their business model"
    )
    action.add_argument("--public", action="store_true", help="print one public catalog")
    action.add_argument(
        "--export", action="store_true", help="write one portable source project to stdout"
    )
    args = parser.parse_args()
    if args.list:
        for example in BY_ID.values():
            print(f"{example.id:30} {example.family:22} {example.policy}")
        print(f"{'mixed':30} {'mixed-process':22} job-shop")
        return
    if args.export or args.public:
        if args.example == "all":
            parser.error("select one example for --export or --public")
        if args.export:
            sys.stdout.buffer.write(project(args.example).to_json() + b"\n")
            return
    ids = (*BY_ID, "mixed") if args.example == "all" else (args.example,)
    total = 0
    failed = []
    for id in ids:
        factory = mixed.define_shop if id == "mixed" else lambda: define_shop(BY_ID[id])
        artifact = build_example(factory, Path(__file__).parent.glob("*.py"))
        if args.public:
            print(json.dumps(public_catalog(artifact), indent=2))
            return
        cases = mixed.scenarios() if id == "mixed" else scenarios(BY_ID[id])
        results = run_cases(artifact, cases)
        failures = [result for result in results if not result.passed]
        total += len(results)
        print(f"{'FAIL' if failures else 'PASS'} {id}: {len(results)} technical/business cases")
        if failures:
            failed.append(id)
            for result in failures:
                print(result.model_dump_json())
    print(f"{len(ids)} shops, {total} cases, {len(failed)} failing shops; all evidence simulated.")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
