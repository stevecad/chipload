"""Container entrypoint. Never invoke this module in a public service process."""

import contextlib
import importlib
import json
import sys
import tempfile
from pathlib import Path

from pydantic import ValidationError

from .check_worker import CheckExecutionError, run_checks
from .model import Shop
from .project import MAX_PROJECT_BYTES, SourceProject, unique_json
from .validation import validate_shop


def main() -> None:
    try:
        data = sys.stdin.buffer.read(MAX_PROJECT_BYTES + 1)
        if len(data) > MAX_PROJECT_BYTES:
            raise ValueError("project exceeds the size limit")
        project = SourceProject.model_validate(unique_json(data))
        with tempfile.TemporaryDirectory(prefix="chipload-source-") as directory:
            root = Path(directory)
            for file in project.files:
                target = root / file.path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(file.content, encoding="utf-8")
            sys.path.insert(0, str(root))
            module, function = project.entrypoint.split(":")
            # Source output is a private diagnostic stream, not the result protocol.
            with contextlib.redirect_stdout(sys.stderr):
                factory = getattr(importlib.import_module(module), function)
                shop = factory()
                if not isinstance(shop, Shop):
                    raise ValueError("entrypoint must return a chipload.Shop")
                result = {"shop": shop.model_dump(mode="json")}
                if any(cap.python_checks for cap in shop.capabilities):
                    validate_shop(shop)
                    result["python_checks"] = run_checks(shop, project)
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    except CheckExecutionError as error:
        print(json.dumps({"diagnostics": [{"code": "python-check-error", "path": error.path,
                                          "message": str(error)}]}))
    except ValidationError as error:
        print(
            json.dumps(
                {
                    "diagnostics": [
                        {
                            "code": "invalid-field",
                            "path": [str(part)[:200] for part in item["loc"][:32]],
                            "message": item["msg"][:1000],
                        }
                        for item in error.errors(
                            include_url=False, include_context=False, include_input=False
                        )[:100]
                    ]
                }
            )
        )
    except Exception as error:
        path = []
        if isinstance(error, SyntaxError):
            path = [Path(error.filename or "").name[:200], str(error.lineno)]
        print(
            json.dumps(
                {
                    "diagnostics": [
                        {
                            "code": "source-error",
                            "path": path,
                            "message": f"{type(error).__name__}: {error}"[:1000],
                        }
                    ]
                }
            )
        )


if __name__ == "__main__":
    main()
