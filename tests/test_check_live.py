import importlib.machinery
import importlib.util
import io
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
