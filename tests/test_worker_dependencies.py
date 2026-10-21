"""Operator lock admission and separation of shop libraries from database authority."""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "worker_dependencies", ROOT / "scripts/worker_dependencies.py"
)
locks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(locks)


class WorkerDependencyTests(unittest.TestCase):
    def test_pins_are_canonical_and_include_all_dependency_bytes(self):
        first = locks.requirements(
            b"# reviewed\nSome_Lib==1.0 \\\n    --hash=sha256:" + b"a" * 64 + b"\n"
        )
        second = locks.requirements(b"some-lib==1.0 --hash=sha256:" + b"a" * 64 + b"\n")
        self.assertEqual(first, second)
        self.assertNotEqual(locks.fingerprint(b"core"), locks.fingerprint(b"core", first))
        self.assertNotEqual(locks.fingerprint(b"core", first), locks.fingerprint(b"other", first))
        self.assertNotEqual(
            locks.fingerprint(b"core", first),
            locks.fingerprint(b"core", first.replace(b"1.0", b"1.1")),
        )

    def test_pip_directives_urls_unpinned_and_duplicate_packages_are_rejected(self):
        suffix = b" --hash=sha256:" + b"a" * 64 + b"\n"
        for invalid in (
            b"",
            b"# empty",
            b"lib==1",
            b"lib>=1" + suffix,
            b"lib==1; python_version>'3'" + suffix,
            b"lib[extra]==1" + suffix,
            b"lib @ https://example.com/w.whl" + suffix,
            b"-r hidden.txt",
            b"--index-url https://example.com",
            b"--trusted-host example.com",
            b"-e .",
            b"../lib==1" + suffix,
            b"chipload==1" + suffix,
            b"lib==1" + suffix + b"LIB==1" + suffix,
            b"lib==1 --hash=md5:" + b"a" * 32,
            b"lib==1" + suffix + b"unfinished\\",
            b"\xff",
            b"#" * (locks.MAX_LOCK_BYTES + 1),
        ):
            with self.subTest(invalid=invalid[:80]), self.assertRaises(ValueError):
                locks.requirements(invalid)

    def test_committed_cadquery_lock_uses_supported_syntax(self):
        canonical = locks.requirements((ROOT / "deploy/worker/cadquery.txt").read_bytes())
        self.assertIn(b"cadquery==2.8.0 ", canonical)
        self.assertIn(b"cadquery-ocp==", canonical)
        self.assertIn(b"numpy==", canonical)
