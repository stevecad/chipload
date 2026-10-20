"""An external evidence service with explicit business conditions and callable contracts."""

from datetime import datetime, timezone
from pathlib import Path

from chipload import (
    Capability,
    Choice,
    Evidence,
    Fact,
    OperationCheck,
    OperationField,
    Process,
    Shop,
    ShopOperation,
)
from chipload.evaluate import EvaluationRequest
from chipload.project import AcceptanceCase, SourceFile, SourceProject

AT = datetime(2026, 10, 20, tzinfo=timezone.utc)


def define_shop():
    policy = (Choice(fact="purpose", allowed=("prototype-verification",)),)
    return Shop(
        id="service-example",
        name="External evidence service example",
        visibility="public",
        processes=(Process(id="document-access", name="Evidence access"),),
        capabilities=(
            Capability(
                id="access-policy",
                name="Prototype evidence access",
                visibility="public",
                process="document-access",
                requirements=policy,
                evidence=("declared-policy",),
            ),
        ),
        evidence=(
            Evidence(
                id="declared-policy",
                name="Example access policy",
                level="declared",
                source="person",
                claim="Example policy for a test service; this does not qualify a physical process.",
                capabilities=("access-policy",),
                requirements=policy,
                recorded_at=AT,
            ),
        ),
        operations=tuple(
            ShopOperation(
                id=identifier,
                name=name,
                visibility="public",
                adapter="evidence-service",
                command=command,
                effect=effect,
                effect_description=description,
                inputs=(
                    OperationField(name="record-reference", kind="text"),
                    OperationField(name="purpose", kind="text"),
                ),
                outputs=(
                    OperationField(name="external-reference", kind="text"),
                    OperationField(name="status", kind="text"),
                ),
                checks=(
                    OperationCheck(
                        target_kind="capability",
                        target="access-policy",
                        inputs=("purpose",),
                        minimum_evidence="declared",
                    ),
                ),
            )
            for identifier, name, command, effect, description in (
                (
                    "read-evidence-status",
                    "Read evidence status",
                    "read-status",
                    "read",
                    "Read a status and external reference from the evidence service.",
                ),
                (
                    "request-evidence-review",
                    "Request an evidence review",
                    "request-review",
                    "write",
                    "Create a review request in the configured evidence service.",
                ),
            )
        ),
    )


def project():
    return SourceProject(
        shop_id="service-example",
        entrypoint="shop:define_shop",
        files=(SourceFile(path="shop.py", content=Path(__file__).read_text()),),
        tests=tuple(
            AcceptanceCase(
                id=name,
                request=EvaluationRequest(
                    target_kind="capability",
                    target="access-policy",
                    facts=(Fact(name="purpose", value=purpose),),
                    minimum_evidence="declared",
                ),
                at=AT,
                expected=outcome,
            )
            for name, purpose, outcome in (
                ("prototype", "prototype-verification", "supported"),
                ("production", "production-release", "unsupported"),
            )
        ),
    )


if __name__ == "__main__":
    import sys

    sys.stdout.buffer.write(project().to_json() + b"\n")
