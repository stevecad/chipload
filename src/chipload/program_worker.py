"""Container-only Python execution with a data protocol to the trusted broker."""

import contextlib
import json
import sys
import tempfile
from pathlib import Path

from .client import Client, ServiceError, json_value
from .limits import MAX_JSON_BYTES
from .program_model import MAX_PROGRAM_BYTES, MAX_PROGRAM_RESULT, RunPython
from .project import unique_json


def main():
    incoming, outgoing = sys.stdin.buffer, sys.stdout

    def send(value):
        outgoing.write(json.dumps(value, ensure_ascii=True, allow_nan=False) + "\n")
        outgoing.flush()

    def call(operation, request):
        send({"kind": "call", "operation": operation, "request": request})
        response = unique_json(incoming.readline(MAX_JSON_BYTES + 1))
        if response["ok"]:
            return response["value"]
        raise ServiceError(response["status"], response["detail"])

    try:
        request = RunPython.model_validate(unique_json(incoming.readline(MAX_PROGRAM_BYTES + 1)))
        with tempfile.TemporaryDirectory(prefix="chipload-program-") as directory:
            root = Path(directory)
            for file in request.files:
                path = root / file.path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(file.content, encoding="utf-8")
            sys.path.insert(0, str(root))
            namespace = {
                "__name__": "__main__",
                "client": Client(call),
                "inputs": request.inputs,
                "result": None,
            }
            # The container, not Python builtins or AST filtering, is the code boundary.
            with contextlib.redirect_stdout(sys.stderr):
                exec(compile(request.code, "main.py", "exec"), namespace)
                value = json_value(namespace["result"])
                encoded = json.dumps(value, ensure_ascii=True, allow_nan=False).encode()
                if len(encoded) > MAX_PROGRAM_RESULT:
                    raise ValueError("program result exceeds its byte limit")
            send({"kind": "result", "value": value})
    except Exception as error:
        send({"kind": "error", "error": f"{type(error).__name__}: {error}"[:1000]})


if __name__ == "__main__":
    main()
