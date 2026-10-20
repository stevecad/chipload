import json
import time
from pathlib import Path

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from test_lifecycle import FixtureBuilder

from chipload.access import ACTIONS, Grant, Principal
from chipload.auth import ServerSettings, TokenVerifier
from chipload.lifecycle import BuildProcessor, Repository, ShopService
from chipload.operations import Operations
from chipload.published import PublishedService
from examples.export_project import project
from examples.heat_treatment import AT, observations


class IdentityFixture:
    def __init__(self, directory, *, base="http://localhost:8088"):
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private.public_key()))
        self.jwk.update(kid="test-key", use="sig", alg="RS256")
        self.keys = [self.jwk]
        self.fetches = 0
        self.unavailable = False
        owner = Principal(
            subject="owner", grants=(Grant(shop_id="thermal-example", actions=ACTIONS),)
        )
        editor = Principal(
            subject="editor",
            grants=(
                Grant(
                    shop_id="thermal-example",
                    actions=("source:read", "source:write", "build:run", "build:read"),
                ),
            ),
        )
        self.settings = ServerSettings(
            mode="development",
            database=Path(directory) / "platform.db",
            resource_url=base + "/mcp",
            issuer="https://identity.example/realms/chipload",
            jwks_url="https://identity.example/realms/chipload/protocol/openid-connect/certs",
            bindings=(owner, editor),
        )

        def transport(request):
            self.fetches += 1
            assert str(request.url) == self.settings.jwks_url
            if self.unavailable:
                raise httpx.ConnectError("test outage")
            return httpx.Response(200, json={"keys": self.keys})

        self.client = httpx.Client(transport=httpx.MockTransport(transport))
        self.verifier = TokenVerifier(self.settings, client=self.client)

    def token(self, subject="owner", *, roles=("shop:admin",), changes=None, headers=None):
        current = int(time.time())
        claims = {
            "iss": self.settings.issuer,
            "aud": self.settings.resource_url,
            "sub": subject,
            "iat": current,
            "exp": current + 300,
            "typ": "Bearer",
            "resource_access": {"chipload": {"roles": list(roles)}},
        }
        claims.update(changes or {})
        return jwt.encode(
            claims, self.private, algorithm="RS256", headers=headers or {"kid": "test-key"}
        )

    def operations(self):
        service = ShopService(Repository(self.settings.database))
        source = project()
        owner = self.settings.bindings[0]
        saved = service.save_source(owner, source, expected_head=None)
        build = service.request_build(owner, source.shop_id, saved.revision)
        BuildProcessor(service.repository, FixtureBuilder()).run(build.id)
        service.publish(owner, source.shop_id, build.id, expected_generation=0)
        published = PublishedService(service, clock=lambda: AT)
        published.write_observations(owner, source.shop_id, observations(), expected_revision=None)
        return Operations(service, published)
