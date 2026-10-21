"""OpenBao operator state protects initialization intent and storage ownership."""

import base64
import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from secrets_support import archive, fixture_args, script


@unittest.skipUnless(shutil.which("gpg"), "requires GnuPG public-key validation")
class SecretsStackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="chipload-secrets-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.gpg, self.args = fixture_args(self.root)
        self.addCleanup(
            lambda: subprocess.run(
                ["gpgconf", "--homedir", str(self.root / "gnupg"), "--kill", "gpg-agent"],
                capture_output=True,
            )
        )
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(script.executor.runtime, "docker_socket"))
        self.patches.enter_context(patch.object(script.executor, "private_network"))
        self.owner = self.patches.enter_context(
            patch.object(script, "ownership", return_value=None)
        )
        script.configure(self.args)
        self.state = self.args.state
        self.value = script.read_json(self.state / "deployment.json")

    def test_existing_state_and_project_cannot_be_replaced(self):
        before = (self.state / "deployment.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "state already exists"):
            script.configure(self.args)
        self.assertEqual((self.state / "deployment.json").read_bytes(), before)
        self.args.state = self.root / "other"
        self.owner.return_value = {"container": "already exists"}
        with self.assertRaisesRegex(ValueError, "already has a container"):
            script.configure(self.args)
        self.assertFalse(self.args.state.exists())

    def test_private_pgp_material_and_duplicate_recipients_are_rejected(self):
        path = self.root / "private.pgp"
        archive.write(
            path,
            subprocess.check_output([*self.gpg, "--export-secret-keys"], stderr=subprocess.DEVNULL),
        )
        with self.assertRaisesRegex(ValueError, "public PGP key"):
            script.pgp_public(path)
        self.args.state = self.root / "duplicates"
        self.args.pgp_key = [self.args.pgp_key[0]] * 3
        with self.assertRaisesRegex(ValueError, "distinct share recipients"):
            script.configure(self.args)
        self.assertFalse(self.args.state.exists())

    def test_unknown_initialization_outcome_never_replays_or_exposes_response(self):
        with patch.object(
            script, "request", side_effect=[(200, {"initialized": False}), OSError("private reply")]
        ):
            with self.assertRaises(OSError):
                script.initialize(self.state, self.value, {})
        self.assertTrue((self.state / script.PENDING).exists())
        with patch.object(script, "request") as request:
            with self.assertRaisesRegex(ValueError, "uncertain"):
                script.initialize(self.state, self.value, {})
            request.assert_not_called()
        with (
            patch("sys.argv", ["secrets-stack", "status"]),
            patch.object(script, "operate", side_effect=OSError("private reply")),
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            with self.assertRaises(SystemExit):
                script.main()
        self.assertNotIn("private reply", errors.getvalue())

    def test_durable_encrypted_response_finishes_offline_without_second_init(self):
        packet = base64.b64encode(b"\x85" + b"x" * 150).decode()
        replace = archive.replace_json

        def interrupted(path, value):
            replace(path, value)
            if path.name == "bootstrap.json":
                raise OSError("lost local completion")

        with (
            patch.object(
                script,
                "request",
                side_effect=[
                    (200, {"initialized": False}),
                    (200, {"keys_base64": [packet] * 3, "root_token": packet}),
                ],
            ),
            patch.object(archive, "replace_json", side_effect=interrupted),
        ):
            with self.assertRaises(OSError):
                script.initialize(self.state, self.value, {})
        (self.state / "data/raft/snapshots").mkdir(parents=True, mode=0o700)
        archive.write(self.state / "data/raft/raft.db", "fixture")
        archive.write(self.state / "data/vault.db", "fixture")
        with patch.object(script, "request") as request:
            script.initialize(self.state, self.value, None)
            request.assert_not_called()
        self.assertFalse((self.state / script.PENDING).exists())
        self.assertEqual(
            json.loads((self.state / "initialized.json").read_bytes())["owner"],
            script.ownership_key(self.state, self.value),
        )

    def test_missing_or_shared_storage_and_changed_config_refuse_before_compose(self):
        _, command, environment, _ = script.deployment(self.state)
        args = type(self.args)(state=self.state, action="up")
        with patch.object(script, "compose") as compose:
            (self.state / "data").chmod(0o755)
            with self.assertRaises(ValueError):
                script.operate(args)
            (self.state / "data").chmod(0o700)
            archive.write(self.state / "initialized.json", "{}")
            with self.assertRaisesRegex(ValueError, "storage is incomplete"):
                script.operate(args)
            (self.state / "initialized.json").unlink()
            (self.state / "server.hcl").write_text('listener "tcp" { tls_disable = true }')
            with self.assertRaisesRegex(ValueError, "differs from the deployment"):
                script.operate(args)
            compose.assert_not_called()

    def test_journal_from_other_deployment_and_plain_bootstrap_response_refuse(self):
        marker = self.state / script.PENDING
        archive.replace_json(marker, {"version": 1, "owner": "different"})
        with self.assertRaisesRegex(ValueError, "another deployment"):
            script.initialize(self.state, self.value, None)
        marker.unlink()
        plaintext = base64.b64encode(b"unseal value").decode()
        with patch.object(
            script,
            "request",
            side_effect=[
                (200, {"initialized": False}),
                (200, {"keys_base64": [plaintext] * 3, "root_token": plaintext}),
            ],
        ):
            with self.assertRaisesRegex(ValueError, "PGP packets"):
                script.initialize(self.state, self.value, {})
        self.assertFalse((self.state / "bootstrap.json").exists())

    def test_pinned_identity_survives_sealing_and_rejects_other_or_empty_storage(self):
        path = self.state / "initialized.json"
        archive.replace_json(
            path, {"version": 1, "owner": script.ownership_key(self.state, self.value)}
        )
        script.check_identity(
            self.state,
            self.value,
            {"initialized": True, "sealed": False, "cluster_id": "cluster-one"},
        )
        script.check_identity(self.state, self.value, {"initialized": True, "sealed": True})
        for result in (
            {"initialized": False, "sealed": True},
            {"initialized": True, "sealed": False, "cluster_id": "cluster-two"},
        ):
            with self.assertRaises(script.SecretsError):
                script.check_identity(self.state, self.value, result)
        self.assertEqual(script.read_json(path)["cluster_id"], "cluster-one")
