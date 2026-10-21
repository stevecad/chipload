"""Reject foreign or stale issuer builds before creating an account writer."""

import copy
import json
import unittest
from unittest.mock import Mock

from acme_support import acme


class ACMEEnvironmentTests(unittest.TestCase):
    def record(self):
        return {
            "Id": "sha256:" + "a" * 64,
            "Config": {
                "Labels": {
                    "org.chipload.acme-role": "issuer",
                    "org.chipload.acme-sha256": acme.ISSUER_SHA256,
                }
            },
        }

    def resolve(self, records):
        command = Mock(return_value=json.dumps(records).encode())
        return acme.resolve_image(acme.IMAGES["issuer"], acme.ISSUER_SHA256, command, 123)

    def test_selected_reference_resolves_to_the_immutable_image_id(self):
        record = self.record()
        command = Mock(return_value=json.dumps([record]).encode())
        self.assertEqual(
            acme.resolve_image(acme.IMAGES["issuer"], acme.ISSUER_SHA256, command, 123),
            record["Id"],
        )
        command.assert_called_once_with(["docker", "image", "inspect", acme.IMAGES["issuer"]], 123)

    def test_a_reference_cannot_select_a_foreign_or_stale_build(self):
        for key, value in (
            ("org.chipload.acme-role", "worker"),
            ("org.chipload.acme-sha256", "b" * 64),
        ):
            record = self.record()
            record["Config"]["Labels"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.resolve([record])

    def test_missing_identity_and_malformed_inspection_are_rejected(self):
        record = self.record()
        for value in ([], {}, [record, record], [None]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.resolve(value)
        for changes in (
            {"Id": "mutable:tag"},
            {"Id": None},
            {"Config": None},
            {"Config": {}},
            {"Config": {"Labels": None}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.resolve([{**copy.deepcopy(record), **changes}])
