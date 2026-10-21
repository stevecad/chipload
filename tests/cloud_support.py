"""Real TLS cloud fixture; identities are signed by a test Keycloak-shaped issuer."""

import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path

import uvicorn
from server_support import IdentityFixture
from tls_support import stack

from chipload.cloud import create_cloud_app, initialize_cloud
from chipload.cloud_client import CloudClient
from chipload.directory_model import CloudSettings, OwnerBinding
from chipload.federation_wire import EgressPolicy


class CloudServer:
    def __init__(self, peers):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        stack.local_certificates(self.root / "tls", {"api": "localhost", "identity": "localhost"})
        self.socket = self.listener()
        self.port = self.socket.getsockname()[1]
        self.origin = f"https://localhost:{self.port}"
        self.identity = IdentityFixture(self.root, base=self.origin)
        ca = self.root / "peer-ca.pem"
        ca.write_bytes(b"\n".join(peer.policy.ca_file.read_bytes() for peer in peers))
        self.settings = CloudSettings(directory_id=uuid.uuid4().hex,
            identity=self.identity.settings.model_copy(update={"mode": "production", "database": self.root / "private/cloud.db", "bindings": ()}),
            egress=EgressPolicy(private_origins=tuple(peer.origin for peer in peers), ca_file=ca),
            bindings=tuple(OwnerBinding(subject=f"owner-{index}", instance_id=peer.settings.instance_id) for index, peer in enumerate(peers)))
        initialize_cloud(self.settings)
        self.policy = EgressPolicy(private_origins=(self.origin,), ca_file=self.root / "tls/identity-ca.pem", seconds=15)
        self.client = CloudClient(self.origin, self.settings.directory_id, policy=self.policy)
        self.start()

    @staticmethod
    def listener(port=0):
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        return listener

    def start(self):
        self.app = create_cloud_app(self.settings, verifier=self.identity.verifier)
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level="critical", access_log=False,
            proxy_headers=False, timeout_graceful_shutdown=2,
            ssl_certfile=str(self.root / "tls/certificates/api.crt"), ssl_keyfile=str(self.root / "tls/certificates/api.key")))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True)
        self.thread.start()
        until = time.monotonic() + 10
        while not self.server.started and self.thread.is_alive() and time.monotonic() < until:
            time.sleep(.01)
        if not self.server.started:
            raise RuntimeError("cloud TLS fixture failed to start")

    def owner(self, index):
        return CloudClient(self.origin, self.settings.directory_id, policy=self.policy,
                           token=lambda: self.identity.token(f"owner-{index}"))

    def stop(self):
        self.server.should_exit = True
        self.thread.join(10)
        if self.thread.is_alive():
            raise RuntimeError("cloud TLS fixture failed to stop")
        self.socket.close()

    def restart(self):
        self.stop()
        self.socket = self.listener(self.port)
        self.start()

    def close(self):
        if self.thread.is_alive():
            self.stop()
        self.identity.client.close()
        self.temporary.cleanup()
