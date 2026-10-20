"""Local operator CLI. Remote clients must use an authenticated service boundary."""

import argparse
import json
import sys
from pathlib import Path

from .access import ACTIONS, Grant, Principal
from .lifecycle import BuildProcessor, Repository, ShopService
from .project import MAX_PROJECT_BYTES, SourceProject, unique_json
from .quotas import QuotaPolicy
from .reference import sdk_reference
from .sandbox import DockerBuilder, WorkerEnvironment


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--database", type=Path, default=Path(".local/chipload.db"))
    commands = command.add_subparsers(dest="action", required=True)
    save = commands.add_parser("save", help="save a source bundle without executing it")
    save.add_argument("project", type=Path)
    save.add_argument("--expected-head")
    source = commands.add_parser("source")
    source.add_argument("shop")
    source.add_argument("--revision")
    build = commands.add_parser("build")
    build.add_argument("shop")
    build.add_argument("revision")
    build.add_argument("--image", help="immutable local image ID; default: .local/worker-image")
    inspect = commands.add_parser("inspect-build")
    inspect.add_argument("shop")
    inspect.add_argument("build_id")
    for name in ("publish", "rollback", "withdraw"):
        change = commands.add_parser(name)
        change.add_argument("shop")
        if name != "withdraw":
            change.add_argument("build_id")
        change.add_argument("--expected-generation", required=True, type=int)
    for name in ("catalog", "publication", "audit"):
        commands.add_parser(name).add_argument("shop")
    commands.add_parser("sdk-reference")
    recover = commands.add_parser(
        "recover-builds", help="remove expired workers and fail their jobs"
    )
    recover.add_argument("--image")
    status = commands.add_parser("quotas", help="read platform quotas, request limits, and usage")
    status.add_argument("--shop")
    status.add_argument("--identity")
    quotas = commands.add_parser("set-quotas", help="update quotas for every API and broker")
    quotas.add_argument("policy", type=Path)
    quotas.add_argument("--expected-generation", type=int, required=True)
    return command


def main() -> None:
    args = parser().parse_args()
    if args.action == "sdk-reference":
        print(json.dumps(sdk_reference(), indent=2))
        return
    try:
        service = ShopService(Repository(args.database))
        if args.action in {"quotas", "set-quotas"}:
            if args.action == "quotas":
                output = service.repository.read_quotas(shop=args.shop, identity=args.identity)
            else:
                with args.policy.open("rb") as file:
                    data = file.read(16 * 1024 + 1)
                if len(data) > 16 * 1024:
                    raise ValueError("quota policy exceeds the size limit")
                policy = QuotaPolicy.model_validate(unique_json(data))
                output = service.repository.set_quotas(
                    policy, expected_generation=args.expected_generation
                )
            print(json.dumps(output, indent=2))
            return
        if args.action == "save":
            with args.project.open("rb") as file:
                data = file.read(MAX_PROJECT_BYTES + 1)
            if len(data) > MAX_PROJECT_BYTES:
                raise ValueError("source bundle exceeds the size limit")
            project = SourceProject.model_validate(unique_json(data))
            shop = project.shop_id
        else:
            shop = getattr(args, "shop", None)
        # Possession of this local database and Docker socket grants operator authority.
        # Never construct this principal from a remote caller's request fields.
        principal = Principal(
            subject="local-operator",
            grants=(Grant(shop_id=shop, actions=ACTIONS),) if shop else (),
        )
        if args.action == "save":
            revision = service.save_source(principal, project, expected_head=args.expected_head)
            output = {"shop_id": shop, "revision": revision.revision}
        elif args.action == "source":
            output = service.get_source(principal, shop, args.revision).model_dump(mode="json")
        elif args.action in {"build", "recover-builds"}:
            image = args.image or Path(".local/worker-image").read_text().strip()
            processor = BuildProcessor(
                service.repository, DockerBuilder(WorkerEnvironment.inspect(image))
            )
            if args.action == "recover-builds":
                output = {"recovered": processor.recover_expired()}
            else:
                record = service.request_build(principal, shop, args.revision)
                processor.run(record.id)
                record = service.get_build(principal, shop, record.id)
                output = record.model_dump(mode="json", exclude={"report": {"artifact"}})
                print(json.dumps(output, indent=2))
                if record.state != "succeeded":
                    raise SystemExit(1)
                return
        elif args.action == "inspect-build":
            output = service.get_build(principal, shop, args.build_id).model_dump(mode="json")
        elif args.action == "publication":
            output = service.get_publication(principal, shop).model_dump(mode="json")
        elif args.action == "audit":
            output = service.audit(principal, shop)
        elif args.action == "catalog":
            output = service.catalog(shop)
        else:
            method = getattr(service, args.action)
            arguments = (principal, shop)
            if args.action != "withdraw":
                arguments += (args.build_id,)
            output = method(*arguments, expected_generation=args.expected_generation).model_dump(
                mode="json"
            )
        print(json.dumps(output, indent=2))
    except (OSError, ValueError, LookupError) as error:
        print(f"chipload: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
