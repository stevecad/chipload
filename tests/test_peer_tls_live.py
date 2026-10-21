"""Peer identity and publication through the shipped TLS/Keycloak containers."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from credentials_support import credentials
from tls_support import ROOT, stack

from chipload.auth import ServerSettings
from chipload.client import Client, ServiceError
from chipload.federation_wire import EgressPolicy
from chipload.peer import PeerClient
from examples.federation_shops import project, query


@unittest.skipUnless(os.environ.get("CHIPLOAD_TLS_TESTS") == "1", "requires worker, runtime and TLS images")
class PeerTLSLiveTests(unittest.TestCase):
    def test_direct_peer_identity_survives_restart_and_queries_follow_withdrawal(self):
        with tempfile.TemporaryDirectory(prefix="chipload-peer-tls-") as temporary:
            state = Path(temporary) / "state"
            listeners = [socket.socket(), socket.socket()]
            for listener in listeners:
                listener.bind(("127.0.0.1", 0))
            port, admin_port = (listener.getsockname()[1] for listener in listeners)
            for listener in listeners:
                listener.close()
            name = "chipload-peer-tls-" + uuid.uuid4().hex[:12]
            cli = [sys.executable, str(ROOT / "scripts/tls-stack"), "--state", str(state)]

            def run(*arguments):
                completed = subprocess.run(arguments, capture_output=True, text=True, timeout=240)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                return completed.stdout.strip()

            run(*cli, "init", "--local", "--port", str(port), "--admin-port", str(admin_port),
                "--name", name, "--shop", "supplier-far")
            settings = ServerSettings.model_validate_json((state / "runtime.json").read_bytes())
            identifier = uuid.uuid4().hex
            staged = state / "runtime-peer.json"
            stack.write(staged, settings.model_copy(update={"instance_id": identifier}).model_dump_json(indent=2))
            staged.replace(state / "runtime.json")
            try:
                run(*cli, "up")
                connection = json.loads((state / "client.json").read_bytes())
                values = stack.configuration(json.loads((state / "deployment.json").read_bytes()))
                secret = Path(connection["client_secret_file"]).read_text()
                cached = {}

                def token():
                    if time.monotonic() >= cached.get("until", 0):
                        status, value = credentials.agent_token(state, values, secret)
                        self.assertEqual(status, 200, "agent token request failed")
                        cached.update(token=value["access_token"], until=time.monotonic() + max(1, value["expires_in"] - 5))
                    return cached["token"]

                origin = connection["resource_url"][:-4]
                shop = Client.connect(origin, ca_file=connection["ca_file"], token=token).shop("supplier-far")
                source = project()
                saved = shop.save(source, expected_head=None)
                job = shop.build(saved["revision"], request_key="peer-initial-build")
                built = job
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    try:
                        built = shop.build_result(job["id"])
                    except ServiceError as error:
                        if error.status not in (429, 503):
                            raise
                    else:
                        if built["state"] in {"succeeded", "failed"}:
                            break
                    time.sleep(1)
                self.assertEqual(built["state"], "succeeded", "isolated listing build failed")
                shop.publish(job["id"], expected_generation=0)
                peer = PeerClient(origin, identifier, policy=EgressPolicy(
                    private_origins=(origin,), ca_file=Path(connection["ca_file"]), seconds=10,
                ))
                self.assertEqual(peer.descriptor().instance_id, identifier)
                catalog = peer.catalog("supplier-far")
                result = peer.evaluate_listing("supplier-far", "service", query(revision=catalog.revision), expected_generation=1)
                self.assertEqual(result.value["outcome"], "supported")
                run(*cli, "down")
                run(*cli, "up")
                self.assertEqual(peer.descriptor().instance_id, identifier)
                self.assertEqual(peer.catalog("supplier-far"), catalog)
                shop.withdraw(expected_generation=1)
                with self.assertRaises(ServiceError) as removed:
                    peer.catalog("supplier-far")
                self.assertEqual(removed.exception.status, 404)
            finally:
                run(*cli, "down")
                volumes = run("docker", "volume", "ls", "-q", "--filter",
                              f"label=com.docker.compose.project={name}").split()
                if volumes:
                    run("docker", "volume", "rm", *volumes)
