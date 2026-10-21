import json
import tempfile
import time
import unittest

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError
from server_support import IdentityFixture

from chipload.access import Forbidden, require
from chipload.auth import AuthenticationError, IdentityUnavailable, ServerSettings


class AuthTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.identity = IdentityFixture(directory.name)
        self.addCleanup(self.identity.client.close)
        self.verifier = self.identity.verifier

    def test_valid_keycloak_style_token_gets_only_explicit_shop_grants(self):
        principal = self.verifier.verify(self.identity.token())
        require(principal, "thermal-example", "publication:write")
        with self.assertRaises(Forbidden):
            require(principal, "another-shop", "source:read")
        self.assertEqual(self.verifier.verify(self.identity.token("unbound")).grants, ())

    def test_role_and_shop_grant_are_both_required(self):
        editor = self.verifier.verify(self.identity.token("editor"))
        require(editor, "thermal-example", "source:write")
        with self.assertRaises(Forbidden):
            require(editor, "thermal-example", "publication:write")
        reader = self.verifier.verify(self.identity.token(roles=("shop:read",)))
        require(reader, "thermal-example", "source:read")
        with self.assertRaises(Forbidden):
            require(reader, "thermal-example", "source:write")
        no_role = self.verifier.verify(self.identity.token(roles=()))
        with self.assertRaises(Forbidden):
            require(no_role, "thermal-example", "source:read")

    def test_invalid_issuer_audience_dates_and_id_tokens_are_rejected(self):
        current = int(time.time())
        for changes in (
            {"iss": "https://attacker.example"},
            {"aud": "another-resource"},
            {"exp": current - 1},
            {"iat": current + 30},
            {"nbf": current + 30},
            {"exp": str(current + 100)},
            {"iat": True},
            {"sub": ""},
            {"typ": "ID"},
            {"typ": None},
            {"exp": current + 10000},
        ):
            with self.subTest(changes=changes), self.assertRaises(AuthenticationError):
                self.verifier.verify(self.identity.token(changes=changes))

    def test_signature_algorithm_and_unknown_keys_fail_closed(self):
        wrong = jwt.encode(
            {"sub": "owner"},
            "test-secret-padding-at-least-thirty-two-bytes",
            algorithm="HS256",
            headers={"kid": "test-key"},
        )
        with self.assertRaises(AuthenticationError):
            self.verifier.verify(wrong)
        token = self.identity.token()
        header, payload, signature = token.split(".")
        with self.assertRaises(AuthenticationError):
            self.verifier.verify(
                f"{header}.{payload}.{('A' if signature[0] != 'A' else 'B') + signature[1:]}"
            )
        with self.assertRaises(AuthenticationError):
            self.verifier.verify(self.identity.token(headers={"kid": "missing"}))

    def test_key_cache_rotation_and_refresh_flood_limit(self):
        self.verifier.verify(self.identity.token())
        self.verifier.verify(self.identity.token())
        self.assertEqual(self.identity.fetches, 1)
        for index in range(20):
            with self.assertRaises(AuthenticationError):
                self.verifier.verify(self.identity.token(headers={"kid": f"missing-{index}"}))
        self.assertEqual(self.identity.fetches, 1)
        self.identity.keys = [{**self.identity.jwk, "kid": "rotated"}]
        self.verifier._last_attempt -= 6
        self.verifier.verify(self.identity.token(headers={"kid": "rotated"}))
        self.assertEqual(self.identity.fetches, 2)

    def test_expired_key_cache_does_not_accept_tokens_during_outage(self):
        self.verifier.verify(self.identity.token())
        self.identity.unavailable = True
        self.verifier._valid_until = 0
        self.verifier._last_attempt -= 6
        with self.assertRaises(IdentityUnavailable):
            self.verifier.verify(self.identity.token())

    def test_distinct_signing_keys_overlap_then_retire_after_cache_refresh(self):
        old = self.identity.token()
        before = self.verifier.verify(old)
        replacement = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(replacement.public_key()))
        key.update(kid="replacement", alg="RS256", use="sig")
        self.identity.keys.append(key)
        self.identity.private = replacement
        new = self.identity.token(headers={"kid": "replacement"})
        self.verifier._last_attempt -= 6
        self.assertEqual(self.verifier.verify(new), before)
        self.assertEqual(self.verifier.verify(old), before)
        self.identity.keys = [key]
        # Local verification retains a usable cached key until its bounded expiry.
        self.assertEqual(self.verifier.verify(old), before)
        self.verifier._valid_until = 0
        self.verifier._last_attempt -= 6
        self.assertEqual(self.verifier.verify(new), before)
        with self.assertRaises(AuthenticationError):
            self.verifier.verify(old)

    def test_endpoints_require_secure_production_configuration(self):
        values = self.identity.settings.model_dump()
        with self.assertRaises(ValidationError):
            ServerSettings.model_validate({**values, "mode": "production"})
        for endpoint in ("http://external.example/mcp", "https://user:password@example.com/mcp"):
            with self.assertRaises(ValidationError):
                ServerSettings.model_validate({**values, "resource_url": endpoint})


if __name__ == "__main__":
    unittest.main()
