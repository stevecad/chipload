import importlib.machinery
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("check_live", str(ROOT / "scripts/check-live"))
check_live = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(check_live)


class LiveCheckRunnerTests(unittest.TestCase):
    def test_standalone_command_imports_checkout_examples_and_enables_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "tests").mkdir()
            (root / "examples").mkdir()
            (root / "examples/__init__.py").write_text("VALUE = 'fixture checkout'\n")
            command = root / "scripts/check-live"
            shutil.copyfile(ROOT / "scripts/check-live", command)
            flags, names = check_live.PROFILES["python"]
            for name in names:
                (root / f"tests/test_{name}_live.py").write_text(
                    "import os, unittest, examples\n"
                    "class Example(unittest.TestCase):\n"
                    "    def test_import_and_flags(self):\n"
                    "        self.assertEqual(examples.VALUE, 'fixture checkout')\n"
                    f"        for flag in {flags!r}:\n"
                    "            self.assertEqual(os.environ['CHIPLOAD_' + flag + '_TESTS'], '1')\n"
                )
            env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
            result = subprocess.run(
                [sys.executable, str(command), "python"],
                cwd=root / "scripts", env=env, capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(f"Ran {len(names)} tests", result.stderr)

    def run_case(self, body):
        name = "test_ci_" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / (name + ".py")).write_text(
                "import unittest\nclass Example(unittest.TestCase):\n    def test_case(self):\n        " + body + "\n"
            )
            before = sys.path[:]
            try:
                return check_live.run_suites((name + ".py",), path, stream=io.StringIO())
            finally:
                sys.path[:] = before
                sys.modules.pop(name, None)

    def test_actual_success_and_failure_determine_exit(self):
        self.assertTrue(self.run_case("self.assertEqual(2 + 2, 4)"))
        self.assertFalse(self.run_case("self.fail('failed verification')"))
        self.assertFalse(self.run_case("raise RuntimeError('broken fixture')"))

    def test_missing_prerequisite_cannot_be_reported_as_success(self):
        self.assertFalse(self.run_case("self.skipTest('missing Docker')"))

    def test_removed_or_empty_suites_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            before = sys.path[:]
            try:
                with self.assertRaisesRegex(ValueError, "No tests discovered"):
                    check_live.run_suites(("missing.py",), Path(directory), stream=io.StringIO())
                with self.assertRaisesRegex(ValueError, "No live test suites selected"):
                    check_live.run_suites((), Path(directory), stream=io.StringIO())
            finally:
                sys.path[:] = before


if __name__ == "__main__":
    unittest.main()
