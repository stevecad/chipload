"""Container-only execution of the published source and selected Python rules."""

import contextlib
import importlib
import json
import sys
import tempfile
from pathlib import Path

from .artifact import canonical_json, digest
from .assessment_model import AssessmentWork
from .check_worker import invoke_checks
from .limits import MAX_JSON_BYTES, bounded_json
from .model import Shop


def main():
    work = AssessmentWork.model_validate(bounded_json(sys.stdin.buffer.read(MAX_JSON_BYTES + 1)))
    with tempfile.TemporaryDirectory(prefix="chipload-assessment-") as directory:
        root = Path(directory)
        for file in work.project.files:
            path = root / file.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(file.content, encoding="utf-8")
        sys.path.insert(0, str(root))
        with contextlib.redirect_stdout(sys.stderr):
            module, function = work.project.entrypoint.split(":")
            shop = getattr(importlib.import_module(module), function)()
            if not isinstance(shop, Shop) or digest(canonical_json(shop)) != work.model_sha256:
                raise ValueError("Source did not reproduce its published model")
            plan = {(call.capability, call.check.id): call.check for call in work.checks}
            result = invoke_checks(work.query, plan, "assessment")
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()
