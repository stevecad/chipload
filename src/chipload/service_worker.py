"""Trusted data/service worker. Never executes Python supplied by callers."""

import json
import math
import resource
import sys

from .access import Principal
from .lifecycle import Repository, ShopService
from .limits import bounded_json
from .operations import Operations
from .service_errors import problem
from .service_pool import MAX_INPUT, MAX_OUTPUT


def main(*, activity=None, configuration=None):
    incoming, outgoing = sys.stdin.buffer, sys.stdout.buffer

    def read():
        line = incoming.readline(MAX_INPUT + 1)
        if not line:
            raise EOFError()
        if len(line) > MAX_INPUT or not line.endswith(b"\n"):
            raise ValueError("invalid service frame")
        if activity:
            activity(True)
        frame = bounded_json(line, max_bytes=MAX_INPUT)
        if (
            not isinstance(frame, dict)
            or set(frame) != {"id", "body"}
            or not isinstance(frame["id"], str)
            or len(frame["id"]) != 32
        ):
            raise ValueError("invalid service frame identity")
        return frame["id"], frame["body"]

    def send(status, body):
        data = (
            json.dumps(
                {"id": identifier, "status": status, "body": body},
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        if len(data) > MAX_OUTPUT:
            raise ValueError("service output too large")
        outgoing.write(data)
        outgoing.flush()
        if activity:
            activity(False)

    identifier, config = read()
    if configuration:
        configuration(config)
    operations = Operations(
        ShopService(Repository(config["database"], cache_bytes=config["cache_bytes"]))
    )
    if (
        configuration
        and operations.service.repository.admission.identity != config["admission_identity"]
    ):
        raise ValueError("SDK service repository identity mismatch")
    send(200, {"ready": True})
    while True:
        try:
            # Waiting on an idle pipe consumes no meaningful CPU. Extend the soft
            # lifetime limit before reading/decoding the next bounded request.
            usage = resource.getrusage(resource.RUSAGE_SELF)
            ceiling = math.ceil(usage.ru_utime + usage.ru_stime) + config["cpu_seconds"]
            hard = resource.getrlimit(resource.RLIMIT_CPU)[1]
            if hard >= 0 and ceiling >= hard:
                return
            resource.setrlimit(resource.RLIMIT_CPU, (ceiling, hard))
            identifier, message = read()
        except EOFError:
            return
        try:
            if not isinstance(message, dict) or set(message) != {
                "operation",
                "principal",
                "request",
            }:
                raise ValueError("invalid service request")
            name = message["operation"]
            if not isinstance(name, str) or name not in operations.specs:
                raise ValueError("unknown service operation")
            who = (
                Principal.model_validate(message["principal"])
                if message["principal"] is not None
                else None
            )
            result = operations.call(name, who, message["request"])
            send(200, result)
        except MemoryError:
            # Do not retain a process whose interpreter could be out of memory.
            return
        except Exception as error:
            status, body = problem(error)
            send(status, body)
