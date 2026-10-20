"""Package the trusted heat-treatment example as a portable source bundle."""

import sys
from pathlib import Path

from chipload.project import AcceptanceCase, SourceFile, SourceProject

from .heat_treatment import AT, SCENARIOS, observations


def project() -> SourceProject:
    return SourceProject(
        shop_id="thermal-example",
        entrypoint="thermal:define_shop",
        files=(
            SourceFile(
                path="thermal.py",
                content=Path(__file__).with_name("heat_treatment.py").read_text(),
            ),
        ),
        tests=tuple(
            AcceptanceCase(
                id=case.name,
                request=case.question,
                expected=case.expected,
                reason_codes=(case.reason,),
                at=AT,
                observations=observations(),
            )
            for case in SCENARIOS
        ),
    )


if __name__ == "__main__":
    sys.stdout.buffer.write(project().to_json() + b"\n")
