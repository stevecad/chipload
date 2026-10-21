"""Interrupted flow creation and refusal of changed authentication requirements."""

import copy
import unittest

from secrets_support import script

mfa = script.load("operator_mfa_test", "scripts/operator_mfa.py")


class Provider:
    def __init__(self, lose=None):
        self.flow = None
        self.executions = []
        self.configurations = {}
        self.writes = 0
        self.lose = lose

    def admin(self, path, *, method="GET", data=None, expected=(200,)):
        data = copy.deepcopy(data)
        if method == "GET":
            if path == "/authentication/flows":
                result = (200, [self.flow] if self.flow else [])
            elif path.endswith("/executions"):
                result = (200, sorted(self.executions, key=lambda row: row["priority"]))
            elif "/config/" in path:
                result = (200, self.configurations[path.rsplit("/", 1)[1]])
            else:
                result = (200, self.flow) if self.flow and self.flow["id"] == path.rsplit("/", 1)[1] else (404, {})
        else:
            if path == "/authentication/flows":
                self.flow = data
            elif path == "/authentication/executions":
                self.executions.append({
                    "id": data["id"], "providerId": data["authenticator"],
                    "requirement": data["requirement"], "priority": data["priority"],
                    "level": 0,
                })
            elif path.endswith("/config"):
                execution = next(row for row in self.executions if row["id"] == path.split("/")[-2])
                execution["authenticationConfig"] = data["id"]
                self.configurations[data["id"]] = data
            else:
                raise AssertionError(path)
            self.writes += 1
            if self.writes == self.lose:
                raise OSError("response lost after committed configuration")
            result = (201, {})
        if result[0] not in expected:
            raise AssertionError((path, result[0], expected))
        return copy.deepcopy(result)


class MFATests(unittest.TestCase):
    def test_resume_after_each_lost_creation_response_preserves_identifiers_without_duplicates(self):
        for lost in range(1, 6):
            with self.subTest(lost=lost):
                provider, identifier = Provider(lose=lost), mfa.new_flow()
                with self.assertRaises(OSError):
                    mfa.ensure(provider, identifier, create=True)
                mfa.ensure(provider, identifier, create=True)
                mfa.ensure(provider, identifier)
                self.assertEqual(provider.writes, 5)
                self.assertEqual(len(provider.executions), 2)
                self.assertEqual(len(provider.configurations), 2)

    def test_missing_or_changed_completed_flow_is_not_repaired(self):
        identifier = mfa.new_flow()
        provider = Provider()
        with self.assertRaisesRegex(ValueError, "differs"):
            mfa.ensure(provider, identifier)
        self.assertEqual(provider.writes, 0)
        mfa.ensure(provider, identifier, create=True)
        original = copy.deepcopy(provider)
        for change in ("optional", "factor", "extra", "order", "nested", "configuration"):
            with self.subTest(change=change):
                provider = copy.deepcopy(original)
                if change == "optional":
                    provider.executions[1]["requirement"] = "ALTERNATIVE"
                elif change == "factor":
                    next(iter(provider.configurations.values()))["config"]["default.reference.value"] = "otp"
                elif change == "extra":
                    provider.executions.append({"id": "cookie", "priority": 0})
                elif change == "order":
                    provider.executions[1]["priority"] = 0
                elif change == "nested":
                    provider.executions[1]["level"] = 1
                else:
                    provider.executions[1].pop("authenticationConfig")
                with self.assertRaises(ValueError):
                    mfa.ensure(provider, identifier)
                self.assertEqual(provider.writes, 5)

    def test_new_creation_does_not_adopt_another_flow_using_the_alias(self):
        identifier = mfa.new_flow()
        provider = Provider()
        flow, _ = mfa.specification(identifier)
        provider.flow = {**flow, "id": mfa.new_flow()}
        with self.assertRaisesRegex(ValueError, "alias belongs"):
            mfa.ensure(provider, identifier, create=True)
        self.assertEqual(provider.writes, 0)
