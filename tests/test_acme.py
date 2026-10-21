"""Issuer custody, deployment ownership and recovery before unattended renewal."""

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from acme_support import ROOT, acme, archive
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization


class ACMETests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.provider = self.root / "provider"
        self.hosts = {"api": "shop.example.test", "identity": "identity.example.test"}
        acme.stack.local_certificates(self.provider, self.hosts)
        self.state = self.root / "issuer"
        self.args = SimpleNamespace(
            state=self.state,
            api_host=self.hosts["api"],
            identity_host=self.hosts["identity"],
            server="https://acme.example.test/dir",
            email="operator@example.test",
            listen_address="127.0.0.1",
            http_port=8080,
            network="bridge",
            renew_days=0,
            accept_terms=True,
            ca_bundle=self.provider / "identity-ca.pem",
            directory_ca=self.provider / "identity-ca.pem",
        )
        acme.initialize(self.args)
        self.values = acme.load(self.state)

    def deployment(self):
        target = self.root / "deployment"
        args = SimpleNamespace(
            state=target,
            local=False,
            name="acme-deployment",
            shop="example",
            api_host=self.hosts["api"],
            identity_host=self.hosts["identity"],
            port=8443,
            admin_port=8444,
            subnet="10.240.1.0/28",
            certificates=self.provider / "certificates",
            ca_bundle=self.provider / "identity-ca.pem",
        )
        with (
            patch.object(acme.stack.subprocess, "check_output", return_value=""),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            acme.stack.initialize(args)
        values = json.loads((target / "deployment.json").read_bytes())
        patches = contextlib.ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(patch.object(acme.stack, "deployment", return_value=([], {}, values)))
        patches.enter_context(
            patch.object(acme.certificates.recovery, "check_containers", return_value=(False, {}))
        )
        patches.enter_context(
            patch.object(
                acme.certificates.recovery,
                "records",
                return_value=[
                    {
                        "Name": "acme-deployment_keycloak-data",
                        "CreatedAt": "original",
                        "Driver": "local",
                        "Labels": {},
                    }
                ],
            )
        )
        acme.delivery.bind(self.state, target, self.values, acme.certificates)
        return target

    def bundle(self, name):
        target = self.root / name
        shutil.copytree(self.provider, target)
        key = serialization.load_pem_private_key(
            (target / "recovery/local-ca-key.pem").read_bytes(), password=None
        )
        for role in self.hosts:
            path = target / f"certificates/{role}.crt"
            old = x509.load_pem_x509_certificate(path.read_bytes())
            builder = (
                x509.CertificateBuilder()
                .subject_name(old.subject)
                .issuer_name(old.issuer)
                .public_key(old.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(old.not_valid_before_utc)
                .not_valid_after(old.not_valid_after_utc)
            )
            for extension in old.extensions:
                builder = builder.add_extension(extension.value, critical=extension.critical)
            path.write_bytes(
                builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)
            )
        return target

    def test_configuration_rejects_insecure_endpoints_challenges_and_unaccepted_terms(self):
        for changes in (
            {"server": "http://acme.example.test/dir"},
            {"server": "https://secret@acme.example.test/dir"},
            {"server": "https://acme.example.test/dir?secret=value"},
            {"server": "https://acme.example.test:70000/dir"},
            {"accept_terms": False},
            {"network": "host"},
            {"network": "container:other"},
            {"api_host": "*.example.test"},
            {"api_host": "shop.localhost"},
            {"http_port": True},
            {"image": "goacme/lego:latest"},
            {"renew_days": -1},
            {"listen_address": "239.0.0.1"},
        ):
            with self.subTest(changes=changes), self.assertRaises((ValueError, TypeError)):
                acme.configuration({**self.values, **changes})
        changed = json.loads((self.state / "client.json").read_bytes())
        changed["servers"]["issuer"]["tlsSkipVerify"] = True
        (self.state / "client.json").write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, "client settings changed"):
            acme.load(self.state)

    def test_private_initialization_preserves_accounts_and_refuses_replacement(self):
        for path in self.state.rglob("*"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
        with self.assertRaisesRegex(ValueError, "state exists"):
            acme.initialize(self.args)
        key = self.state / "client/accounts/server/shop.key"
        archive.write(key, (self.provider / "certificates/api.key").read_bytes())
        acme.check_account(self.state, record=True)
        acme.check_account(self.state)
        key.unlink()
        with self.assertRaisesRegex(ValueError, "key storage changed"):
            acme.check_account(self.state)

    def test_partial_delivery_resumes_only_its_own_durable_installation(self):
        target = self.deployment()
        bundle = self.bundle("renewed")
        replace = acme.certificates.replace_file

        def interrupted(*arguments):
            replace(*arguments)
            raise RuntimeError("power loss")

        with patch.object(acme.certificates, "replace_file", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                acme.delivery.deliver(self.state, bundle, self.values, acme.certificates)
        intent = acme.delivery.read(self.state / acme.delivery.PENDING)
        self.assertEqual(acme.certificates.read_journal(target)["update_id"], intent["update_id"])
        acme.delivery.deliver(self.state, bundle, self.values, acme.certificates)
        self.assertEqual(acme.certificates.hashes(target), acme.certificates.hashes(bundle))
        self.assertFalse((target / acme.certificates.MARKER).exists())
        self.assertFalse((self.state / acme.delivery.PENDING).exists())
        self.assertEqual(
            acme.delivery.read(target / "certificates.json")["update_id"], intent["update_id"]
        )

    def test_lost_install_reply_does_not_restart_a_completed_delivery(self):
        target = self.deployment()
        bundle = self.bundle("renewed")
        install = acme.certificates.install

        def lost(*arguments, **options):
            install(*arguments, **options)
            raise RuntimeError("lost reply")

        with patch.object(acme.certificates, "install", side_effect=lost) as calls:
            with self.assertRaises(RuntimeError):
                acme.delivery.deliver(self.state, bundle, self.values, acme.certificates)
            acme.delivery.deliver(self.state, bundle, self.values, acme.certificates)
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(
            acme.delivery.read(self.state / acme.delivery.BINDING)["material"],
            acme.certificates.hashes(target),
        )

    def test_manual_certificate_change_and_foreign_installation_stop_automatic_delivery(self):
        target = self.deployment()
        original = (target / "certificates/api.crt").read_bytes()
        (target / "certificates/api.crt").write_bytes(original + b"\n")
        with self.assertRaisesRegex(ValueError, "outside this issuer"):
            acme.delivery.preflight(self.state, self.values, acme.certificates)
        (target / "certificates/api.crt").write_bytes(original)
        bundle = self.bundle("manual")
        with patch.object(acme.certificates, "replace_file", side_effect=RuntimeError("paused")):
            with self.assertRaises(RuntimeError):
                acme.certificates.install(
                    target,
                    certificates=bundle / "certificates",
                    ca_bundle=bundle / "identity-ca.pem",
                )
        with self.assertRaisesRegex(ValueError, "another certificate"):
            acme.delivery.preflight(self.state, self.values, acme.certificates)

    def test_manual_replacement_of_issuer_candidate_is_not_silently_replaced_again(self):
        target = self.deployment()
        with patch.object(acme.certificates, "replace_file", side_effect=RuntimeError("paused")):
            with self.assertRaises(RuntimeError):
                acme.delivery.deliver(
                    self.state, self.bundle("first"), self.values, acme.certificates
                )
            manual = self.bundle("manual")
            with self.assertRaises(RuntimeError):
                acme.certificates.install(
                    target,
                    action="replace-pending",
                    certificates=manual / "certificates",
                    ca_bundle=manual / "identity-ca.pem",
                )
        with self.assertRaisesRegex(ValueError, "operator changed"):
            acme.delivery.preflight(self.state, self.values, acme.certificates)

    def test_installation_checks_expected_journal_identity_and_candidate_under_its_lock(self):
        target = self.deployment()
        with patch.object(acme.certificates, "replace_file", side_effect=RuntimeError("paused")):
            with self.assertRaises(RuntimeError):
                acme.delivery.deliver(
                    self.state, self.bundle("renewed"), self.values, acme.certificates
                )
        before = acme.certificates.hashes(target)
        with self.assertRaisesRegex(ValueError, "owns the journal"):
            acme.certificates.install(target, action="resume", expected_update_id="0" * 32)
        with self.assertRaisesRegex(ValueError, "candidate changed"):
            acme.certificates.install(target, action="resume", expected_replacement={})
        self.assertEqual(before, acme.certificates.hashes(target))

    def test_fresh_installation_checks_material_again_after_acquiring_its_lock(self):
        target = self.deployment()
        bundle = self.bundle("renewed")
        install = acme.certificates.install
        changed = (target / "certificates/api.crt").read_bytes() + b"\n"

        def concurrent(*arguments, **options):
            (target / "certificates/api.crt").write_bytes(changed)
            return install(*arguments, **options)

        with patch.object(acme.certificates, "install", side_effect=concurrent):
            with self.assertRaisesRegex(ValueError, "changed before installation"):
                acme.delivery.deliver(self.state, bundle, self.values, acme.certificates)
        self.assertEqual((target / "certificates/api.crt").read_bytes(), changed)
        self.assertFalse((target / acme.certificates.MARKER).exists())

    def test_unbind_preserves_pending_installation_for_explicit_operator_recovery(self):
        target = self.deployment()
        bundle = self.bundle("renewed")
        with patch.object(acme.certificates, "replace_file", side_effect=RuntimeError("paused")):
            with self.assertRaises(RuntimeError):
                acme.delivery.deliver(self.state, bundle, self.values, acme.certificates)
        journal = (target / acme.certificates.MARKER).read_bytes()
        before = acme.certificates.hashes(target)
        # Even a crash between disabling the binding and removing delivery intent
        # must leave automatic delivery disabled.
        unlink = Path.unlink

        def interrupted(path, **options):
            if path.name == acme.delivery.PENDING:
                raise RuntimeError("power loss")
            unlink(path, **options)

        with patch.object(Path, "unlink", interrupted), self.assertRaises(RuntimeError):
            acme.delivery.unbind(self.state)
        self.assertIsNone(acme.delivery.preflight(self.state, self.values, acme.certificates))
        acme.delivery.unbind(self.state)
        acme.delivery.deliver(self.state, bundle, self.values, acme.certificates)
        self.assertEqual(before, acme.certificates.hashes(target))
        self.assertEqual(journal, (target / acme.certificates.MARKER).read_bytes())
        acme.certificates.install(target, action="resume")
        acme.delivery.bind(self.state, target, self.values, acme.certificates)
        self.assertIsNotNone(acme.delivery.preflight(self.state, self.values, acme.certificates))

    @unittest.skipUnless(shutil.which("systemd-analyze"), "requires systemd unit validation")
    def test_timer_arguments_preserve_literal_paths_and_units_are_valid(self):
        target = self.deployment()
        state = self.root / 'issuer $USER %n "quoted"'
        state.mkdir(mode=0o700)
        paths = acme.timer.write(state, self.root / "units", "chipload-acme-fixture", ROOT, target)
        result = subprocess.run(
            ["systemd-analyze", "--user", "verify", *(str(path) for path in paths)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        text = paths[0].read_text()
        self.assertIn("ExecStart=:", text)
        self.assertIn("$USER %%n", text)
        self.assertIn('\\"quoted\\"', text)
        with self.assertRaisesRegex(ValueError, "already exist"):
            acme.timer.write(state, self.root / "units", "chipload-acme-fixture", ROOT, target)
        with self.assertRaises(ValueError):
            acme.timer.quote("/path\nExecStart=unexpected")


if __name__ == "__main__":
    unittest.main()
