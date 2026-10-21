"""Signing rotation retains its exact remote intent across lost responses."""

import contextlib
import copy
import io
import json
import sys
import unittest
from unittest.mock import patch

import test_credentials
from credentials_support import credentials as script


class SigningTests(unittest.TestCase):
    def setUp(self):
        test_credentials.CredentialTests.setUp(self)
        self.active = "old-key"
        self.providers = {
            "old": {
                "id": "old",
                "name": "rsa-generated",
                "parentId": "realm-id",
                "providerId": "rsa-generated",
                "providerType": script.signing.TYPE,
                "config": {
                    "priority": ["100"],
                    "algorithm": ["RS256"],
                    "keySize": ["2048"],
                    "active": ["true"],
                    "enabled": ["true"],
                    "privateKey": ["DO-NOT-SAVE"],
                },
            }
        }
        self.mutations = []
        self.interrupt = None
        self.clock = 1000
        self.api_running = True
        self.compose.side_effect = self.compose_call
        self.patches.enter_context(patch.object(script.recovery, "records", side_effect=self.records))
        self.patches.enter_context(
            patch.object(script.signing, "now", side_effect=lambda: self.clock)
        )
        self.patches.enter_context(patch.object(script.signing.Rotation, "verify_agent"))
        self.patches.enter_context(patch.object(script, "request", side_effect=self.request))

    def records(self, kind, name):
        if kind == "volume":
            return [self.volume]
        return [{"Config": {"Labels": {"com.docker.compose.service": "api"}},
                 "State": {"Running": self.api_running, "Paused": False, "Restarting": False}}]

    def compose_call(self, *args, **kwargs):
        if args[2] == "stop" and args[-1] == "api":
            self.api_running = False
        if args[2] == "up" and args[-1] == "api":
            self.api_running = True

    def request(self, state, values, path, *, method="GET", data=None, **kwargs):
        signing = script.signing
        if path == signing.REALM:
            return 200, {"id": "realm-id"}
        if path == signing.REALM + "/keys":
            return 200, {
                "active": {"RS256": self.active},
                "keys": [
                    {"providerId": p, "kid": p + "-key", "algorithm": "RS256"}
                    for p in self.providers
                ],
            }
        if path == signing.CERTS:
            return 200, {
                "keys": [
                    {"kid": p + "-key"}
                    for p, c in self.providers.items()
                    if c["config"].get("enabled") == ["true"]
                ]
            }
        if "?type=" in path:
            return 200, copy.deepcopy(list(self.providers.values()))
        if path == signing.PROVIDERS and method == "POST":
            self.assertNotIn(data["id"], self.providers)
            self.providers[data["id"]] = copy.deepcopy(data)
            result = (201, None)
        else:
            identifier = path.rsplit("/", 1)[-1]
            if method == "GET":
                return (
                    (200, copy.deepcopy(self.providers[identifier]))
                    if identifier in self.providers
                    else (404, {})
                )
            if method == "PUT":
                self.providers[identifier] = copy.deepcopy(data)
                if identifier != "old" and data["config"]["active"] == ["true"]:
                    self.active = identifier + "-key"
            elif method == "DELETE":
                del self.providers[identifier]
            else:
                self.fail("unexpected signing request")
            result = (204, None)
        self.mutations.append((method, path, copy.deepcopy(data)))
        if self.interrupt and self.interrupt(method, path, data):
            raise OSError("lost committed response")
        return result

    def test_create_activate_passivate_and_delete_losses_resume_without_new_keys(self):
        for phase in ("create", "activate", "passivate"):
            self.interrupt = lambda method, path, data: (
                (phase == "create" and method == "POST")
                or (phase == "activate" and method == "PUT" and not path.endswith("/old"))
                or (phase == "passivate" and method == "PUT" and path.endswith("/old"))
            )
            with self.assertRaises(OSError):
                script.rotate(
                    self.state,
                    "signing" if phase == "create" else "resume",
                    resume=phase != "create",
                    retain_days=2,
                )
            self.assertEqual(len(self.providers), 2)
            self.assertNotIn("DO-NOT-SAVE", (self.state / script.MARKER).read_text())
        self.interrupt = None
        script.rotate(self.state, "resume", resume=True)
        record = json.loads((self.state / "signing.json").read_bytes())
        self.assertEqual(record["retire_after"], self.clock + 2 * 86400)
        self.assertEqual(self.providers["old"]["config"]["active"], ["false"])
        self.assertEqual(self.providers["old"]["config"]["enabled"], ["true"])
        self.assertEqual(sum(method == "POST" for method, _, _ in self.mutations), 1)
        self.clock = record["retire_after"]
        for method in ("PUT", "DELETE"):
            self.interrupt = lambda m, p, d: m == method
            with self.assertRaises(OSError):
                script.rotate(
                    self.state,
                    "signing-retire" if method == "PUT" else "resume",
                    resume=method == "DELETE",
                )
        self.interrupt = None
        script.rotate(self.state, "resume", resume=True)
        self.assertEqual(len(self.providers), 1)
        self.assertEqual(
            json.loads((self.state / "signing.json").read_bytes())["status"], "retired"
        )
        self.assertFalse((self.state / script.MARKER).exists())

    def test_retention_and_new_rotation_refuse_before_any_mutation(self):
        script.rotate(self.state, "signing")
        before = copy.deepcopy(self.mutations)
        for kind in ("signing-retire", "signing"):
            with self.assertRaises(script.CredentialError):
                script.rotate(self.state, kind)
            self.assertFalse((self.state / script.MARKER).exists())
            self.assertEqual(self.mutations, before)

    def test_replaced_keys_and_other_admin_changes_block_resume(self):
        self.interrupt = lambda m, p, d: m == "POST"
        with self.assertRaises(OSError):
            script.rotate(self.state, "signing")
        self.interrupt = None
        self.active = "unrelated-key"
        with self.assertRaisesRegex(ValueError, "changed outside"):
            script.rotate(self.state, "resume", resume=True)
        self.active = "old-key"
        self.providers["old"]["config"]["keySize"] = ["4096"]
        with self.assertRaisesRegex(ValueError, "changed outside"):
            script.rotate(self.state, "resume", resume=True)

    def test_corrupted_retirement_phase_and_reactivated_provider_cannot_delete_keys(self):
        script.rotate(self.state, "signing", retain_days=1)
        record = json.loads((self.state / "signing.json").read_bytes())
        self.clock = record["retire_after"]
        self.providers["old"]["config"]["active"] = ["true"]
        with self.assertRaisesRegex(ValueError, "no longer passive"):
            script.rotate(self.state, "signing-retire")
        self.assertFalse((self.state / script.MARKER).exists())
        self.providers["old"]["config"]["active"] = ["false"]
        self.interrupt = lambda m, p, d: m == "PUT"
        with self.assertRaises(OSError):
            script.rotate(self.state, "signing-retire")
        before = copy.deepcopy(self.mutations)
        journal = json.loads((self.state / script.MARKER).read_bytes())
        journal["phase"] = "corrupt"
        script.archive.replace_json(self.state / script.MARKER, journal)
        with self.assertRaisesRegex(ValueError, "invalid signing retirement phase"):
            script.rotate(self.state, "resume", resume=True)
        self.assertEqual(self.mutations, before)
        self.assertIn("old", self.providers)

    def test_emergency_revocation_requires_exact_previous_key_before_changes(self):
        script.rotate(self.state, "signing")
        before = copy.deepcopy(self.mutations)
        record = json.loads((self.state / "signing.json").read_bytes())
        for key in (None, "", "another-key", record["new_kid"]):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "key ID"):
                script.rotate(self.state, "signing-revoke", key_id=key)
            self.assertFalse((self.state / script.MARKER).exists())
            self.assertEqual(self.mutations, before)
            self.assertTrue(self.api_running)
        self.providers["old"]["config"]["active"] = ["true"]
        with self.assertRaisesRegex(ValueError, "no longer passive"):
            script.rotate(self.state, "signing-revoke", key_id="old-key")

    def test_emergency_revocation_stops_cached_api_and_resumes_lost_disable_and_delete(self):
        script.rotate(self.state, "signing")
        original = json.loads((self.state / "signing.json").read_bytes())
        self.assertLess(self.clock, original["retire_after"])
        self.interrupt = lambda method, path, data: method == "PUT"
        with self.assertRaises(OSError):
            script.rotate(self.state, "signing-revoke", key_id="old-key")
        self.assertFalse(self.api_running)
        self.assertEqual(self.providers["old"]["config"]["enabled"], ["false"])
        journal = json.loads((self.state / script.MARKER).read_bytes())
        self.assertEqual(journal["signing"]["revocation"], {"key_id": "old-key", "requested_at": self.clock})
        with self.assertRaisesRegex(ValueError, "rotation is incomplete"):
            script.stack.require_ready(self.state)
        with self.assertRaisesRegex(ValueError, "only when starting"):
            script.rotate(self.state, "resume", resume=True, key_id="another-key")
        self.interrupt = lambda method, path, data: method == "DELETE"
        with self.assertRaises(OSError):
            script.rotate(self.state, "resume", resume=True)
        self.assertTrue(self.api_running)
        self.assertNotIn("old", self.providers)
        self.interrupt = None
        script.rotate(self.state, "resume", resume=True)
        record = json.loads((self.state / "signing.json").read_bytes())
        self.assertEqual(record["revocation"], {"key_id": "old-key", "requested_at": self.clock, "completed_at": self.clock})
        self.assertEqual(record["retire_after"], original["retire_after"])
        self.assertEqual(record["status"], "retired")
        self.assertEqual(sum(m == "POST" for m, _, _ in self.mutations), 1)
        self.assertFalse((self.state / script.MARKER).exists())

    def test_unconfirmed_api_stop_cannot_disable_a_key_and_corrupt_intent_cannot_resume(self):
        script.rotate(self.state, "signing")
        before = copy.deepcopy(self.mutations)
        self.compose.side_effect = None
        with self.assertRaisesRegex(ValueError, "API stop must be confirmed"):
            script.rotate(self.state, "signing-revoke", key_id="old-key")
        self.assertEqual(self.mutations, before)
        journal = json.loads((self.state / script.MARKER).read_bytes())
        journal["signing"]["revocation"]["key_id"] = journal["signing"]["new_kid"]
        script.archive.replace_json(self.state / script.MARKER, journal)
        with self.assertRaisesRegex(ValueError, "invalid signing revocation intent"):
            script.rotate(self.state, "resume", resume=True)
        self.assertEqual(self.mutations, before)

    def test_cli_keeps_revocation_and_resume_intent_separate(self):
        for args in (("revoke-signing",), ("--key-id", "old-key", "resume"),
                     ("--key-id", "old-key", "rotate-signing")):
            with self.subTest(args=args), patch.object(sys, "argv", ["tls-credentials", *args]), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    script.main()
                self.assertEqual(error.exception.code, 2)
        with patch.object(sys, "argv", ["tls-credentials", "--key-id", "old-key", "revoke-signing"]), \
             patch.object(script, "rotate") as rotate, patch.object(script.os, "getuid", return_value=1000), \
             contextlib.redirect_stdout(io.StringIO()):
            script.main()
        self.assertEqual(rotate.call_args.args[1], "signing-revoke")
        self.assertEqual(rotate.call_args.kwargs["key_id"], "old-key")
