"""Verify OpenBao image identity before granting it persistent secrets storage."""

import copy
import json
import unittest
from unittest.mock import patch

from secrets_environment_support import environment
from secrets_support import script


class SecretsEnvironmentTests(unittest.TestCase):
    def record(self):
        return {
            "Id": "sha256:" + "a" * 64,
            "Config": {
                "Labels": {
                    "org.chipload.secrets-role": "openbao",
                    "org.chipload.secrets-sha256": script.IMAGE_SHA256,
                },
            },
        }

    def resolve(self, records):
        with patch.object(environment.subprocess, "check_output", return_value=json.dumps(records)):
            return environment.resolve_image(script.IMAGE, script.IMAGE_SHA256)

    def test_build_reference_resolves_to_an_immutable_image(self):
        record = self.record()
        self.assertEqual(self.resolve([record]), record["Id"])

    def test_foreign_and_stale_builds_cannot_supply_secrets_images(self):
        for key, value in (
            ("org.chipload.secrets-role", "worker"),
            ("org.chipload.secrets-sha256", "b" * 64),
        ):
            record = self.record()
            record["Config"]["Labels"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.resolve([record])

    def test_malformed_inspection_and_mutable_identity_refuse(self):
        record = self.record()
        for records in ([], {}, [None], [record, record]):
            with self.subTest(records=records), self.assertRaises(ValueError):
                self.resolve(records)
        for changes in (
            {"Id": "image:tag"},
            {"Id": None},
            {"Config": None},
            {"Config": {}},
            {"Config": {"Labels": None}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.resolve([{**copy.deepcopy(record), **changes}])

    def test_ownership_requires_the_build_and_immutable_creation_reference(self):
        record = self.record()
        record["Image"] = record.pop("Id")
        record["Config"]["Image"] = record["Image"]
        record["Config"]["Labels"]["org.chipload.secrets-owner"] = "fixture-owner"

        def inspect(value):
            with patch.object(
                script.subprocess,
                "check_output",
                side_effect=["container-id\n", json.dumps([value])],
            ):
                return script.ownership({"name": "fixture"}, "fixture-owner")

        self.assertEqual(inspect(record), record)
        for key, value in (
            ("Image", "image:tag"),
            ("Image", "sha256:" + "b" * 64),
            ("Labels", {**record["Config"]["Labels"], "org.chipload.secrets-sha256": "old"}),
        ):
            changed = copy.deepcopy(record)
            changed["Config"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                inspect(changed)
