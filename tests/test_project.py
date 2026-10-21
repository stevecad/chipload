import unittest
from unittest.mock import patch

from pydantic import ValidationError

from chipload.artifact import canonical_json, sdk_fingerprint
from chipload.project import SourceFile, SourceProject, unique_json
from chipload.reference import sdk_reference
from chipload.sandbox import DockerBuilder, WorkerEnvironment


class ProjectTests(unittest.TestCase):
    def project(self, files=None, **changes):
        return SourceProject(
            shop_id="example",
            entrypoint="shop:define",
            files=files or (SourceFile(path="shop.py", content="def define(): pass\n"),),
            **changes,
        )

    def test_paths_cannot_escape_or_replace_the_sdk(self):
        for path in (
            "../shop.py",
            "/shop.py",
            "a/../../shop.py",
            "a//b.py",
            r"a\b.py",
            ".env",
            "shop.py\0",
            "a/./shop.py",
            "chipload/model.py",
            "chipload.py",
            "binary.so",
            "a:" + "/b.py",
        ):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                SourceFile(path=path, content="")

    def test_duplicate_paths_and_file_directory_collisions_are_rejected(self):
        for paths in (
            ("shop.py", "shop.py"),
            ("shop.py", "SHOP.py"),
            ("shop.py", "x.py", "x.py/data.json"),
        ):
            with self.subTest(paths=paths), self.assertRaises(ValidationError):
                self.project(tuple(SourceFile(path=p, content="") for p in paths))

    def test_entrypoint_must_exist_but_saving_does_not_parse_or_execute_source(self):
        self.project((SourceFile(path="shop.py", content="not even valid python !!!"),))
        with self.assertRaises(ValidationError):
            self.project((SourceFile(path="another.py", content=""),))

    def test_source_identity_is_independent_of_file_order(self):
        files = (
            SourceFile(path="shop.py", content="from components import define"),
            SourceFile(path="components.py", content="def define(): pass"),
        )
        self.assertEqual(self.project(files).revision, self.project(files[::-1]).revision)
        changed = (files[0].model_copy(update={"content": "changed"}), files[1])
        self.assertNotEqual(self.project(files).revision, self.project(changed).revision)

    def test_source_size_and_count_are_bounded(self):
        with self.assertRaises(ValidationError):
            SourceFile(path="shop.py", content="x" * (128 * 1024 + 1))
        with self.assertRaises(ValidationError):
            self.project(tuple(SourceFile(path=f"f{i}.py", content="") for i in range(129)))
        files = (SourceFile(path="shop.py", content="x" * (128 * 1024)),)
        files += tuple(SourceFile(path=f"f{i}.py", content="x" * (128 * 1024)) for i in range(17))
        with self.assertRaises(ValidationError):
            self.project(files)

    def test_duplicate_protocol_keys_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            unique_json(b'{"shop": {}, "shop": {}}')

    def test_reference_exposes_real_schemas_and_units(self):
        reference = sdk_reference()
        self.assertIn("catalogs", reference["schemas"]["shop"]["properties"])
        self.assertIn("files", reference["schemas"]["source_project"]["properties"])
        self.assertEqual(reference["units"]["Cel"]["offset"], "5463/20")

    def test_worker_with_a_different_sdk_cannot_be_used(self):
        environment = WorkerEnvironment("sha256:" + "a" * 64, "b" * 64, "c" * 64, "3.12.15")
        with self.assertRaisesRegex(ValueError, "SDKs differ"):
            DockerBuilder(environment)

    def test_dependency_pin_preserves_old_revisions_and_refuses_a_different_environment(self):
        source = self.project()
        old = source.model_dump(mode="json")
        self.assertNotIn("dependencies_sha256", old)
        self.assertEqual(source.to_json(), canonical_json(old))
        pinned = self.project(dependencies_sha256="d" * 64)
        self.assertNotEqual(source.revision, pinned.revision)
        self.assertEqual(SourceProject.model_validate_json(pinned.to_json()), pinned)
        environment = WorkerEnvironment(
            "sha256:" + "a" * 64, sdk_fingerprint(), "c" * 64, "3.12.15"
        )
        with patch("chipload.sandbox.subprocess.run") as run:
            report = DockerBuilder(environment).build(pinned)
        run.assert_not_called()
        self.assertEqual(report.status, "failed")
        self.assertEqual(report.diagnostics[0].code, "dependency-mismatch")
        self.assertEqual(report.diagnostics[0].path, ("dependencies_sha256",))
        with self.assertRaises(ValidationError):
            self.project(dependencies_sha256="latest")


if __name__ == "__main__":
    unittest.main()
