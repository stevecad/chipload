"""Independent shop repositories served over actual TLS for federation checks."""

import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path

import uvicorn
from server_support import IdentityFixture
from tls_support import stack

from chipload import BuildIdentity, compile_shop
from chipload.access import ACTIONS, Grant, Principal
from chipload.auth import TokenVerifier
from chipload.federation_wire import EgressPolicy
from chipload.lifecycle import Repository, ShopService
from chipload.operations import Operations
from chipload.peer import PeerClient
from chipload.project import BuildReport, run_cases
from chipload.published import PublishedService
from chipload.server import create_app
from examples.federation_shops import AT, define_shop, project


class PeerServer:
    def __init__(self, *, profile="supplier-far", shop=None, instance_id=None, managed=False, bindings=None):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        stack.local_certificates(self.root / "tls", {"api": "localhost", "identity": "localhost"})
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.origin = f"https://localhost:{self.socket.getsockname()[1]}"
        self.identity = IdentityFixture(self.root / "state", base=self.origin)
        self.owner = Principal(subject="owner", grants=(Grant(shop_id=profile, actions=ACTIONS),))
        self.settings = self.identity.settings.model_copy(update={"mode": "production", "instance_id": instance_id or uuid.uuid4().hex,
            "bindings": bindings if bindings is not None else (self.owner,)})
        self.service = ShopService(Repository(self.settings.database))
        self.published = PublishedService(self.service, clock=lambda: AT)
        self.operations = Operations(self.service, self.published)
        self.profile = profile
        self.build(shop or define_shop(profile))
        app = create_app(self.settings, operations=None if managed else self.operations,
                         verifier=TokenVerifier(self.settings, client=self.identity.client))
        self.server = uvicorn.Server(uvicorn.Config(
            app, log_level="error", access_log=False, proxy_headers=False,
            ssl_certfile=str(self.root / "tls/certificates/api.crt"),
            ssl_keyfile=str(self.root / "tls/certificates/api.key"),
        ))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        if not self.server.started:
            self.close()
            raise RuntimeError("fixture TLS server did not start")
        self.policy = EgressPolicy(private_origins=(self.origin,), ca_file=self.root / "tls/identity-ca.pem", seconds=5)
        self.client = PeerClient(self.origin, self.settings.instance_id, policy=self.policy)

    def build(self, shop):
        source = project(self.profile)
        if not getattr(self, "saved", False):
            self.service.save_source(self.owner, source, expected_head=None)
            self.saved = True
        job = self.service.request_build(self.owner, self.profile, source.revision)
        claim, _ = self.service.repository.claim(job.id, 60)
        artifact = compile_shop(shop, BuildIdentity(source_sha256=source.revision,
            sdk_sha256="a" * 64, dependencies_sha256="b" * 64, runtime="trusted-peer-fixture"))
        self.service.repository.finish(job.id, claim, BuildReport(
            source_revision=source.revision, worker_image="sha256:" + "c" * 64,
            status="succeeded", artifact=artifact, tests=run_cases(artifact, source.tests),
        ))
        current = self.service.get_publication(self.owner, self.profile)
        self.service.publish(self.owner, self.profile, job.id, expected_generation=current.generation)

    def close(self):
        if hasattr(self, "server"):
            self.server.should_exit = True
            self.thread.join(10)
            if self.thread.is_alive():
                raise RuntimeError("fixture TLS server did not stop")
        self.socket.close()
        self.identity.client.close()
        self.temporary.cleanup()
