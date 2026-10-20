"""TLS receiver fixture with a separate durable effect/receipt database."""

import json
import socket
import sqlite3
import ssl
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from chipload.artifact import canonical_json
from chipload.remote_adapter import ReceiverRequest


class Receiver(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, database, token, *, certificate, key, control=None):
        self.database, self.token = Path(database), token
        self.behavior, self.puts, self.gets, self.cancels = "normal", 0, 0, 0
        self.entered = threading.Event()
        self.receiver_id = "evidence-service"
        self.control = control
        with sqlite3.connect(self.database) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS effects(id TEXT PRIMARY KEY, intent TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, intent TEXT NOT NULL, result BLOB NOT NULL)"
            )
        super().__init__(address, Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        self.socket = context.wrap_socket(self.socket, server_side=True)

    def receipts(self):
        with sqlite3.connect(self.database) as db:
            return db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, value):
        data = canonical_json(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_PUT(self):
        self.handle_call("PUT")

    def do_GET(self):
        self.handle_call("GET")

    def do_POST(self):
        self.handle_call("POST")

    def handle_call(self, method):
        server = self.server
        if server.control:
            value = json.loads(Path(server.control).read_bytes())
            server.behavior, server.token = value["behavior"], value["token"]
        if self.headers.get("Authorization") != "Bearer " + server.token:
            self.reply(401, {})
            return
        parts = self.path.strip("/").split("/")
        if len(parts) not in {3, 4} or parts[:2] != ["v1", "executions"]:
            self.reply(404, {})
            return
        identifier = parts[2]
        cancellation = None
        if method == "POST":
            cancellation = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if parts[-1] != "cancel" or cancellation["receiver_id"] != server.receiver_id:
                self.reply(409, {})
                return
        if method == "PUT":
            server.puts += 1
        elif method == "GET":
            server.gets += 1
        else:
            server.cancels += 1
        if method == "GET" and server.behavior == "lost-and-missing":
            self.reply(404, {})
            return
        if server.behavior == "stall":
            server.entered.set()
            time.sleep(4)
            return
        with sqlite3.connect(server.database) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT intent,result FROM receipts WHERE id=?", (identifier,)
            ).fetchone()
            if method == "POST" and row is None:
                result = {
                    "protocol": "chipload-receiver-1",
                    "receiver_id": server.receiver_id,
                    "execution_id": identifier,
                    "intent_sha256": cancellation["intent_sha256"],
                    "mode": "live",
                    "result": {
                        "state": "cancelled",
                        "code": "cancelled-before-arrival",
                        "outputs": [],
                    },
                }
                encoded = canonical_json(result)
                db.execute(
                    "INSERT INTO receipts VALUES(?,?,?)",
                    (identifier, cancellation["intent_sha256"], encoded),
                )
                row = (cancellation["intent_sha256"], encoded)
            if method == "PUT":
                request = ReceiverRequest.model_validate(
                    json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                )
                if request.execution_id != identifier or request.receiver_id != server.receiver_id:
                    self.reply(409, {})
                    return
                if row:
                    if row[0] != request.intent_sha256:
                        self.reply(409, {})
                        return
                else:
                    rejected = request.deadline <= datetime.now(timezone.utc)
                    result = {
                        "protocol": "chipload-receiver-1",
                        "receiver_id": server.receiver_id,
                        "execution_id": identifier,
                        "intent_sha256": request.intent_sha256,
                        "mode": "live",
                        "result": {
                            "state": "rejected" if rejected else "succeeded",
                            "code": "expired" if rejected else "service-confirmed",
                            "outputs": []
                            if rejected
                            else [
                                {"name": "external-reference", "value": "review-001"},
                                {"name": "status", "value": "recorded"},
                            ],
                        },
                    }
                    if server.behavior == "pending":
                        result["result"] = {
                            "state": "indeterminate",
                            "code": "pending",
                            "outputs": [],
                        }
                    elif not rejected and request.effect == "write":
                        db.execute(
                            "INSERT INTO effects VALUES(?,?)", (identifier, request.intent_sha256)
                        )
                    encoded = canonical_json(result)
                    db.execute(
                        "INSERT INTO receipts VALUES(?,?,?)",
                        (identifier, request.intent_sha256, encoded),
                    )
                    row = (request.intent_sha256, encoded)
            if row is None or server.behavior == "missing":
                self.reply(404, {})
                return
            result = json.loads(row[1])
            if method == "POST":
                body = cancellation
                if (
                    parts[-1] != "cancel"
                    or body["intent_sha256"] != row[0]
                    or body["receiver_id"] != server.receiver_id
                ):
                    self.reply(409, {})
                    return
                if result["result"]["state"] == "indeterminate":
                    result["result"] = {
                        "state": "cancelled",
                        "code": "cancelled-before-service-action",
                        "outputs": [],
                    }
                    db.execute(
                        "UPDATE receipts SET result=? WHERE id=?",
                        (canonical_json(result), identifier),
                    )
            db.commit()
        server.entered.set()
        if server.behavior in {"lost-response", "lost-and-missing"} and method == "PUT":
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        if server.behavior == "redirect":
            self.send_response(307)
            self.send_header("Location", "https://must-not-follow.invalid/exfiltrate")
            self.end_headers()
            return
        if server.behavior == "wrong-intent":
            result["intent_sha256"] = "f" * 64
        if server.behavior == "credential-output":
            result["result"]["outputs"][0]["value"] = server.token
        if server.behavior == "oversize":
            result["result"]["outputs"][0]["value"] = "x" * (100 * 1024)
        self.reply(200, result)
