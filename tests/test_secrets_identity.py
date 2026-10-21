"""Operator identity boundaries, device polling and recoverable public intent."""

import contextlib
import copy
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from secrets_support import archive, script

identity = script.load("identity_unit", "scripts/secrets-identity")


class IdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chipload-identity-unit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state, self.tls_state = self.root / "secrets", self.root / "identity"
        self.state.mkdir(mode=0o700)
        self.tls_state.mkdir(mode=0o700)
        archive.write(self.tls_state / "deployment.json", "{}")
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.jwk = {
            **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key())),
            "kid": "one",
            "use": "sig",
            "alg": "RS256",
        }
        self.config = {
            "version": 2,
            "cluster_id": "cluster",
            "issuer": "https://identity.example/realms/chipload",
            "audience": "urn:chipload:secrets:cluster",
            "client_id": "chipload-secrets-cluster",
            "client_uuid": "client-uuid",
            "mfa_flow": "b014ecac-539d-47f4-a4ab-82381c8d087b",
            "keys": identity.public_keys({"keys": [self.jwk]}),
        }
        self.grant = {
            "name": "alice",
            "subject": "subject-one",
            "role": "operator-alice",
            "policy": "chipload-operator-alice",
            "administrator": False,
            "workloads": [{"name": "evidence", "auth_mount": "approle"}],
        }
        self.claims = {
            "iss": self.config["issuer"],
            "aud": self.config["audience"],
            "sub": self.grant["subject"],
            "azp": self.config["client_id"],
            "typ": "Bearer",
            "iat": int(time.time()),
            "exp": int(time.time()) + 120,
            "chipload_operator_roles": [self.grant["role"]],
            "amr": ["pwd", "otp"],
        }

    def token(self, claims=None):
        return jwt.encode(
            claims or self.claims, self.key, algorithm="RS256", headers={"kid": "one"}
        )

    def test_signed_tokens_must_match_subject_client_audience_role_type_and_lifetime(self):
        identity.check_jwt(self.config, self.grant, self.token())
        for field, value in (
            ("iss", "https://other/realms/chipload"),
            ("aud", "shop-resource"),
            ("sub", "another-subject"),
            ("azp", "chipload-agent"),
            ("typ", "ID"),
            ("exp", int(time.time()) - 1),
            ("exp", int(time.time()) + 3600),
            ("iat", int(time.time()) + 30),
            ("chipload_operator_roles", ["shop:admin"]),
            ("chipload_operator_roles", "prefix-operator-alice-suffix"),
            ("amr", None),
            ("amr", ["pwd"]),
            ("amr", ["otp"]),
            ("amr", "pwd otp"),
            ("amr", [["pwd"], "otp"]),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises((ValueError, jwt.PyJWTError)):
                    identity.check_jwt(
                        self.config, self.grant, self.token({**self.claims, field: value})
                    )
        with self.assertRaisesRegex(ValueError, "not synchronized"):
            identity.check_jwt({**self.config, "keys": {}}, self.grant, self.token())

    def test_private_duplicate_weak_and_non_signing_keys_are_not_accepted(self):
        for keys in (
            [{**self.jwk, "d": "private"}],
            [self.jwk, self.jwk],
            [{**self.jwk, "use": "enc"}],
            [{**self.jwk, "alg": "RS512"}],
        ):
            with self.assertRaises(ValueError):
                identity.public_keys({"keys": keys})
        weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        jwk = {
            **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(weak.public_key())),
            "kid": "weak",
            "use": "sig",
            "alg": "RS256",
        }
        with self.assertRaisesRegex(ValueError, "2048"):
            identity.public_keys({"keys": [jwk]})

    def test_client_normalization_preserves_security_relevant_drift_checks(self):
        expected = identity.client_config(self.config)
        actual = copy.deepcopy(expected)
        for mapper in actual["protocolMappers"]:
            mapper.update(id="generated", consentRequired=False)
        self.assertTrue(identity.same_client(actual, expected))
        for field, value in (
            ("publicClient", False),
            ("directAccessGrantsEnabled", True),
            ("serviceAccountsEnabled", True),
            ("defaultClientScopes", ["roles"]),
            ("fullScopeAllowed", True),
            ("authenticationFlowBindingOverrides", {}),
        ):
            with self.subTest(field=field):
                self.assertFalse(identity.same_client({**actual, field: value}, expected))
        actual["protocolMappers"][1]["config"]["claim.name"] = "another-claim"
        self.assertFalse(identity.same_client(actual, expected))

    def test_openbao_itself_requires_otp_evidence_and_enrollment_precedes_a_grant(self):
        self.assertEqual(identity.role_config(self.config, self.grant)["bound_claims"]["amr"], "otp")
        provider = Mock()
        provider.admin.return_value = (200, [{"type": "password"}])
        with self.assertRaisesRegex(ValueError, "enroll"):
            identity.operator_mfa.require_enrolled(provider, self.grant["subject"])
        provider.admin.return_value = (200, [{"type": "password"}, {"type": "otp"}])
        identity.operator_mfa.require_enrolled(provider, self.grant["subject"])

    def test_existing_client_is_never_recreated_during_upgrade_preflight(self):
        provider = Mock()
        provider.admin.return_value = (404, {})
        with self.assertRaisesRegex(ValueError, "client is missing"):
            identity.ensure_client(provider, self.config)
        provider.admin.assert_called_once_with(
            "/clients/" + self.config["client_uuid"], expected=(200, 404)
        )

    def test_mfa_upgrade_preflight_checks_every_active_grant_without_mutating_services(self):
        legacy = {key: value for key, value in self.config.items() if key != "mfa_flow"}
        legacy["version"] = 1
        archive.replace_json(self.state / identity.RECEIPT, legacy)
        directory = self.state / "operators"
        directory.mkdir(mode=0o700)
        grants = [
            {**self.grant, "status": "active"},
            {**self.grant, "name": "bob", "subject": "subject-two", "role": "operator-bob",
             "policy": "chipload-operator-bob", "status": "active"},
        ]
        for grant in grants:
            archive.replace_json(directory / (grant["name"] + ".json"), grant)
        provider = Mock(state=self.tls_state, values={"identity_host": "identity.example", "port": 443})

        def provider_read(path, **kwargs):
            self.assertEqual(kwargs.get("method", "GET"), "GET")
            if path.startswith("/clients/"):
                return 200, identity.client_config(legacy)
            if path.endswith("/credentials"):
                return 200, [{"type": "otp"}]
            return 200, {"id": path.rsplit("/", 1)[1], "enabled": True}

        responses = {f"auth/{identity.MOUNT}/config": identity.jwt_config(legacy)}
        for grant in grants:
            responses[f"auth/{identity.MOUNT}/role/{grant['name']}"] = identity.role_config(legacy, grant)
            responses["sys/policies/acl/" + grant["policy"]] = {"policy": identity.policy(grant)}

        def bao_read(method, path):
            self.assertEqual(method, "GET")
            return 200, {"data": responses[path]}

        provider.admin.side_effect = provider_read
        api = Mock(state=self.state, cluster=legacy["cluster_id"])
        api.call.side_effect = bao_read
        intent = identity.prepare(api, provider, SimpleNamespace(action="enable-mfa"))
        self.assertEqual(intent["previous_identity"], legacy)
        self.assertEqual(intent["grants"], grants)
        self.assertEqual(intent["identity"]["version"], 2)
        self.assertEqual(intent["identity"]["client_uuid"], legacy["client_uuid"])
        self.assertFalse((self.state / identity.MARKER).exists())
        responses[f"auth/{identity.MOUNT}/role/bob"]["token_policies"] = ["unexpected"]
        with self.assertRaisesRegex(ValueError, "role differs"):
            identity.prepare(api, provider, SimpleNamespace(action="enable-mfa"))
        self.assertFalse((self.state / identity.MARKER).exists())

    def test_pending_mfa_upgrade_uses_new_claim_requirements_for_completed_grants(self):
        legacy = {key: value for key, value in self.config.items() if key != "mfa_flow"}
        legacy["version"] = 1
        archive.replace_json(self.state / identity.RECEIPT, legacy)
        (self.state / "operators").mkdir(mode=0o700)
        active = {**self.grant, "status": "active"}
        archive.replace_json(self.state / "operators/alice.json", active)
        journal = {
            "owner": identity.owner(self.state, self.tls_state), "action": "enable-mfa",
            "identity": self.config, "previous_identity": legacy, "grants": [active],
        }
        identity.save_pending(self.state, self.tls_state, journal)
        configured, grant, _ = identity.login_configuration(self.state, self.tls_state, "alice")
        with self.assertRaisesRegex(ValueError, "does not match"):
            identity.check_jwt(configured, grant, self.token({**self.claims, "amr": ["pwd"]}))

    def test_lost_second_journal_write_is_resumed_from_first_exact_copy(self):
        journal = {
            "version": 1,
            "owner": identity.owner(self.state, self.tls_state),
            "action": "grant",
            "identity": self.config,
            "grant": self.grant,
        }
        replace = archive.replace_json

        def failed_second(path, value):
            if path.parent == self.tls_state:
                raise OSError("second write failed")
            replace(path, value)

        with patch.object(archive, "replace_json", side_effect=failed_second):
            with self.assertRaises(OSError):
                identity.save_pending(self.state, self.tls_state, journal)
        self.assertEqual(identity.pending(self.state, self.tls_state), journal)
        identity.save_pending(self.state, self.tls_state, journal)
        different = {**journal, "action": "revoke"}
        archive.replace_json(self.tls_state / identity.MARKER, different)
        with self.assertRaisesRegex(ValueError, "does not match"):
            identity.pending(self.state, self.tls_state)

    def test_oversized_journal_refuses_before_creating_either_marker(self):
        with self.assertRaisesRegex(ValueError, "exceeds"):
            identity.save_pending(self.state, self.tls_state, {"too_large": "x" * 1024**2})
        self.assertFalse((self.state / identity.MARKER).exists())
        self.assertFalse((self.tls_state / identity.MARKER).exists())

    def test_pending_change_allows_only_completed_unaffected_grants_to_reauthenticate(self):
        archive.replace_json(self.state / identity.RECEIPT, self.config)
        (self.state / "operators").mkdir(mode=0o700)
        active = {**self.grant, "status": "active"}
        archive.replace_json(self.state / "operators/alice.json", active)
        journal = {
            "owner": identity.owner(self.state, self.tls_state),
            "action": "revoke",
            "identity": self.config,
            "grant": {**active, "name": "bob"},
        }
        identity.save_pending(self.state, self.tls_state, journal)
        self.assertEqual(
            identity.login_configuration(self.state, self.tls_state, "alice"),
            (self.config, active, journal),
        )
        journal["grant"] = active
        identity.save_pending(self.state, self.tls_state, journal)
        with self.assertRaisesRegex(ValueError, "another completed grant"):
            identity.login_configuration(self.state, self.tls_state, "alice")
        journal.update(action="sync-keys", grant=None, identity={**self.config, "keys": {}})
        identity.save_pending(self.state, self.tls_state, journal)
        self.assertEqual(
            identity.login_configuration(self.state, self.tls_state, "alice")[0],
            journal["identity"],
        )
        archive.replace_json(self.state / "operators/alice.json", {**active, "status": "revoked"})
        with self.assertRaisesRegex(ValueError, "not active"):
            identity.login_configuration(self.state, self.tls_state, "alice")

    def test_operator_token_paths_cannot_enter_any_mounted_workload_directory(self):
        credential_dir = self.root / "workload"
        credential_dir.mkdir(mode=0o700)
        (self.state / "workloads").mkdir(mode=0o700)
        archive.replace_json(
            self.state / "workloads/example.json", {"credentials": str(credential_dir)}
        )
        for directory in (self.state, self.tls_state, credential_dir):
            with self.assertRaisesRegex(ValueError, "outside"):
                identity.outside_services(directory / "operator-token", self.state, self.tls_state)
        identity.outside_services(self.root / "operator-token", self.state, self.tls_state)

    def test_device_polling_honors_pending_slowdown_and_keeps_device_code_private(self):
        messages, sleeps = [], []
        response = {
            "verification_uri": self.config["issuer"] + "/device",
            "user_code": "ABCD-EFGH",
            "device_code": "private-device-code",
            "interval": 3,
            "expires_in": 120,
        }
        with patch.object(
            identity,
            "device_request",
            side_effect=[
                (200, response),
                (400, {"error": "authorization_pending"}),
                (400, {"error": "slow_down"}),
                (200, {"access_token": "returned-access"}),
            ],
        ):
            token = identity.device_login(
                SimpleNamespace(),
                self.config,
                announce=lambda value, **_: messages.append(value),
                sleep=sleeps.append,
            )
        self.assertEqual(token, "returned-access")
        self.assertEqual(sleeps, [3, 3, 8])
        self.assertNotIn("private-device-code", "".join(messages))
        self.assertIn("ABCD-EFGH", "".join(messages))

    def test_device_address_and_denial_refuse_without_exposing_response(self):
        response = {
            "verification_uri": "https://other.example/device",
            "device_code": "private",
            "user_code": "ABCD",
            "interval": 1,
            "expires_in": 60,
        }
        with patch.object(identity, "device_request", return_value=(200, response)):
            with self.assertRaisesRegex(ValueError, "address differs"):
                identity.device_login(SimpleNamespace(), self.config, announce=Mock())
        response["verification_uri"] = self.config["issuer"] + "/device"
        with patch.object(
            identity,
            "device_request",
            side_effect=[
                (200, response),
                (400, {"error": "access_denied", "private": "must-not-print"}),
            ],
        ):
            with self.assertRaisesRegex(ValueError, "denied or expired"):
                identity.device_login(
                    SimpleNamespace(), self.config, announce=Mock(), sleep=lambda _: None
                )

    def test_prefilled_device_link_must_match_the_verified_address_and_displayed_code(self):
        url = self.config["issuer"] + "/device"
        response = {
            "verification_uri": url,
            "verification_uri_complete": url + "?user_code=ABCD",
            "device_code": "private",
            "user_code": "ABCD",
            "interval": 1,
            "expires_in": 60,
        }
        messages = []
        with patch.object(
            identity, "device_request", side_effect=[(200, response), (200, {"access_token": "ok"})]
        ):
            identity.device_login(
                SimpleNamespace(),
                self.config,
                announce=lambda value, **_: messages.append(value),
                sleep=lambda _: None,
            )
        self.assertIn(url + "?user_code=ABCD", messages[0])
        for complete in (
            "https://other.example/device?user_code=ABCD",
            url + "?user_code=OTHER",
            url + "?user_code=ABCD&user_code=OTHER",
            url + "/other?user_code=ABCD",
            url + "?user_code=ABCD#fragment",
        ):
            with patch.object(
                identity,
                "device_request",
                return_value=(200, {**response, "verification_uri_complete": complete}),
            ):
                with self.assertRaisesRegex(ValueError, "link differs"):
                    identity.device_login(SimpleNamespace(), self.config, announce=Mock())

    def test_cli_failure_hides_validation_and_provider_bearer_values(self):
        with (
            patch(
                "sys.argv",
                [
                    "secrets-identity",
                    "--identity-state",
                    "identity",
                    "login",
                    "--name",
                    "alice",
                    "--output",
                    "file",
                ],
            ),
            patch.object(identity, "operate", side_effect=ValueError("private-bearer-value")),
            contextlib.redirect_stderr(io.StringIO()) as output,
        ):
            with self.assertRaises(SystemExit):
                identity.main()
        self.assertNotIn("private-bearer-value", output.getvalue())
