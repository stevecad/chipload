"""Bounded clients and response accounting for benchmark-deployment.

The controller sends configuration on stdin. Credentials never enter arguments,
reports or response diagnostics. This module also runs inside disposable clients.
"""

import http.client
import json
import math
import re
import socket
import ssl
import statistics
import sys
import time
from collections import Counter
from urllib.parse import urlencode


class LoadFailure(ValueError):
    """A diagnostic composed only from fixed fixture messages."""


def decode(status, data, *, mcp=False):
    """Preserve HTTP and MCP service failures instead of counting HTTP 200 alone."""
    if mcp and status != 200 and data.get("jsonrpc") == "2.0":
        data = data["error"]["data"]
        if data["status"] != status:
            raise ValueError("MCP ingress status disagrees with HTTP")
    if mcp and status == 200:
        result = data["result"]
        data = result["structuredContent"]
        if result.get("isError", False):
            status = data["status"]
            if type(status) is not int or not 400 <= status < 600:
                raise ValueError("invalid MCP service status")
    code = None
    if status != 200:
        code = data.get("error", {}).get("code", "http_error")
        if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,64}", code):
            code = "http_error"
    return status, data, code


def backpressure(status, code):
    return (status == 429 and code == "rate_limited") or (
        status == 503 and code in {"request_capacity", "data_busy"}
    )


def validate(value, case, config):
    # These are known fictional fixtures, not an arbitrary production response.
    encoded = json.dumps(value)
    if "private-equipment.example.invalid" in encoded or '"private_details"' in encoded:
        raise ValueError("private catalog data escaped")
    if case["action"] == "catalog":
        if value["shop"]["id"] != config["shop"] or value["shop"]["name"] != config["name"]:
            raise ValueError("published shop changed during draft authoring")
        revision = value["public_sha256"]
    else:
        revision = value["revision"]
    if revision != config["revision"]:
        raise ValueError("unexpected public revision")
    if case["action"] == "evaluate":
        if value["outcome"] != case["expected"] or value["simulated"] is not True:
            raise ValueError("incorrect capability or business outcome")
        if case["reason"] not in {reason["code"] for reason in value["reasons"]}:
            raise ValueError("missing expected evaluation reason")
    elif case["action"] == "variables":
        findings = {v["definition"]["id"]: v for v in value["variables"]}
        if findings["minimum-production-lot"]["applicability"] != "not-applicable":
            raise ValueError("prototype business condition changed")
    elif case["action"] == "page":
        if {item["id"] for item in value["items"]} != set(config["capabilities"]):
            raise ValueError("unexpected capability page")


def summary(samples, seconds):
    accepted = [
        s["ms"] for s in samples if s["status"] == 200 and not s["error"] and not s["unexpected"]
    ]
    return {
        "requests": len(samples),
        "http_statuses": dict(Counter(str(s["http"]) for s in samples)),
        "service_statuses": dict(Counter(str(s["status"]) for s in samples)),
        "errors": dict(Counter(s["error"] for s in samples if s["error"])),
        "by_operation": dict(Counter(s["operation"] for s in samples)),
        "successes": len(accepted),
        "successful_per_second": round(len(accepted) / seconds, 3),
        "success_ratio": round(len(accepted) / len(samples), 6) if samples else 0,
        "successful_median_ms": round(statistics.median(accepted), 3) if accepted else None,
        "successful_p95_ms": round(sorted(accepted)[math.ceil(len(accepted) * 0.95) - 1], 3)
        if accepted
        else None,
        "slowest": [
            {key: sample[key] for key in ("operation", "ms", "http", "status", "error")}
            for sample in sorted(samples, key=lambda s: s["ms"], reverse=True)[:5]
        ],
        "unexpected": sum(bool(s["unexpected"]) for s in samples),
    }


class Connection(http.client.HTTPSConnection):
    def __init__(self, config, *, identity=False):
        self.address = config["address"]
        super().__init__(
            config["identity_host" if identity else "api_host"],
            config["port"],
            timeout=8,
            context=ssl.create_default_context(cadata=config["ca"]),
        )

    def connect(self):
        raw = socket.create_connection((self.address, self.port), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class Transport:
    def __init__(self, config):
        self.config = config
        self.api = Connection(config)
        self.identity = Connection(config, identity=True)
        self.token = None
        self.expires = 0
        self.tokens = []

    @staticmethod
    def send(connection, method, path, body=None, headers=None):
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            raw = response.read(3 * 1024**2 + 1)
            if len(raw) > 3 * 1024**2:
                raise ValueError("response exceeded limit")
            return response.status, json.loads(raw)
        except BaseException:
            connection.close()
            raise

    def authenticate(self):
        if not self.config.get("secret") or time.monotonic() < self.expires:
            return
        started = time.perf_counter()
        # Token requests are minutes apart; the gateway closes idle connections.
        # Establish a fresh verified connection before sending, without replaying
        # a request after an ambiguous transport failure.
        self.identity.close()
        status, value = self.send(
            self.identity,
            "POST",
            "/realms/chipload/protocol/openid-connect/token",
            urlencode(
                {
                    "grant_type": "client_credentials",
                    "client_id": "chipload-agent",
                    "client_secret": self.config["secret"],
                }
            ),
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.tokens.append({"status": status, "ms": (time.perf_counter() - started) * 1000})
        if status != 200 or not isinstance(value.get("access_token"), str):
            raise ValueError("token acquisition failed")
        self.token = value["access_token"]
        self.expires = time.monotonic() + max(1, min(120, value["expires_in"] - 10))

    def call(self, method, path, *, body=None, mcp=False, headers=None):
        self.authenticate()
        fields = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.token:
            fields["Authorization"] = "Bearer " + self.token
        fields.update(headers or {})
        status, value = self.send(
            self.api, method, path, json.dumps(body) if body is not None else None, fields
        )
        effective, value, code = decode(status, value, mcp=mcp)
        return status, effective, value, code

    def tool(self, name, request):
        return self.call(
            "POST",
            "/mcp",
            mcp=True,
            body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": name,
                    "arguments": {"request": request},
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": self.config["protocol"],
                        "io.modelcontextprotocol/clientCapabilities": {},
                    },
                },
            },
        )

    def close(self):
        self.api.close()
        self.identity.close()


def reader(config):
    transport = Transport(config)
    samples = []
    missed = 0
    started = time.monotonic() + config["start"] - time.time()
    interval = 1 / config["rate"]
    end = started + config["seconds"]
    slots = config["seconds"] * config["rate"]
    slot = 0
    index = 0
    try:
        while slot < slots:
            due = started + config["phase"] + slot * interval
            time.sleep(max(0, due - time.monotonic()))
            if time.monotonic() >= end:
                missed += slots - slot
                break
            case = config["cases"][index % len(config["cases"])]
            index += 1
            # Cycle each fixture through both transports, including odd-length mixes.
            mcp = (index // len(config["cases"])) % 2 == 1
            operation = ("mcp:" if mcp else "rest:") + case["id"]
            begin = time.perf_counter()
            http = status = 0
            error = None
            unexpected = False
            try:
                if config["role"] == "noise":
                    operation = "invalid_token_with_forged_peer"
                    http, status, value, error = transport.call(
                        "GET",
                        f"/v1/shops/{config['shop']}/source",
                        headers={
                            "Authorization": "Bearer invalid-token",
                            "X-Forwarded-For": f"198.51.100.{index % 250 + 1}",
                            "X-Real-IP": "203.0.113.99",
                            "Forwarded": "for=203.0.113.88",
                        },
                    )
                    unexpected = not (
                        (status == 401 and error == "authentication_required")
                        or backpressure(status, error)
                    )
                else:
                    if mcp:
                        http, status, value, error = transport.tool(
                            "discover_shop",
                            {
                                "shop_id": config["shop"],
                                "action": case["action"],
                                "arguments": case["arguments"],
                            },
                        )
                    else:
                        http, status, value, error = transport.call(
                            case["method"], case["path"], body=case.get("body")
                        )
                    if status == 200:
                        validate(value, case, config)
                    unexpected = status != 200 and not backpressure(status, error)
            except Exception as failure:
                error = type(failure).__name__  # Never include response bodies or credentials.
                unexpected = True
            samples.append(
                {
                    "http": http,
                    "status": status,
                    "error": error,
                    "unexpected": unexpected,
                    "operation": operation,
                    "ms": (time.perf_counter() - begin) * 1000,
                }
            )
            slot += 1
            next_slot = min(
                slots,
                max(slot, math.ceil((time.monotonic() - started - config["phase"]) / interval)),
            )
            missed += next_slot - slot
            slot = next_slot
        return {
            "role": config["role"],
            "authenticated": bool(config.get("secret")),
            "missed_slots": missed,
            "samples": samples,
            "tokens": transport.tokens,
        }
    finally:
        transport.close()


AUTHOR = """shop = client.shop(inputs["shop_id"])
source = shop.source()
project = source["project"]
entry = next(file for file in project["files"] if file["path"] == "shop.py")
entry["content"] += "\\n_original_define = define_shop\\ndef define_shop():\\n    return _original_define().model_copy(update={'name': 'Unpublished load draft'})\\n"
saved = shop.save(project, expected_head=source["revision"])
build = shop.build(saved["revision"], request_key="load-draft-build")
result = {"revision": saved["revision"], "build_id": build["id"]}
"""


def author(config):
    transport = Transport(config)
    time.sleep(max(0, config["start"] + 1 - time.time()))
    started = time.time()
    try:
        _, status, run, _ = transport.tool(
            "run_python",
            {
                "shop_id": config["shop"],
                "request_key": "load-draft",
                "code": AUTHOR,
                "inputs": {"shop_id": config["shop"]},
            },
        )
        if status != 200:
            raise LoadFailure("author admission failed")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            _, status, value, _ = transport.tool(
                "read_run",
                {
                    "shop_id": config["shop"],
                    "run_id": run["id"],
                },
            )
            if status == 200 and value["state"] in {
                "succeeded",
                "failed",
                "cancelled",
                "interrupted",
            }:
                if value["state"] != "succeeded":
                    raise LoadFailure("author program did not succeed")
                build_id = value["value"]["build_id"]
                break
            if status not in (200, 429, 503):
                raise LoadFailure("author inspection failed")
            time.sleep(0.5)
        else:
            raise TimeoutError("author program exceeded deadline")
        while time.monotonic() < deadline:
            _, status, build, _ = transport.call(
                "GET", f"/v1/shops/{config['shop']}/builds/{build_id}"
            )
            if status == 200 and build["state"] in {"succeeded", "failed"}:
                if build["state"] != "succeeded":
                    raise LoadFailure("author build failed")
                return {
                    "role": "author",
                    "state": "succeeded",
                    "build_id": build_id,
                    "started_offset_seconds": round(started - config["start"], 3),
                    "finished_offset_seconds": round(time.time() - config["start"], 3),
                    "tokens": transport.tokens,
                }
            if status not in (200, 429, 503):
                raise LoadFailure("build inspection failed")
            time.sleep(0.5)
        raise TimeoutError("author build exceeded deadline")
    finally:
        transport.close()


if __name__ == "__main__":
    config = {}
    try:
        config = json.load(sys.stdin)
        print(json.dumps(author(config) if config["role"] == "author" else reader(config)))
    except Exception as failure:
        import traceback

        print(
            json.dumps(
                {
                    "role": config.get("role"),
                    "state": "failed",
                    "failure": type(failure).__name__,
                    "line": traceback.extract_tb(failure.__traceback__)[-1].lineno,
                    "diagnostic": str(failure) if isinstance(failure, LoadFailure) else None,
                }
            )
        )
        # Keep an unsuccessful author attempt in the measurement report. Its
        # caller can still assess reader availability and the unchanged catalog.
        sys.exit(0 if config.get("role") == "author" else 1)
