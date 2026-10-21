import base64
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from chipload.artifact import canonical_json
from chipload.project import MAX_ASSET_BYTES, SourceAsset, SourceFile, SourceProject
from chipload.source_worker import materialize


class ProjectAssetTests(unittest.TestCase):
    def project(self, *assets, files=None):
        return SourceProject(shop_id="example", entrypoint="shop:define",
                             files=files or (SourceFile(path="shop.py", content="def define(): pass"),), assets=assets)

    def asset(self, path="data/fixture.bin", content=b"\0\xff\x80\n"):
        return SourceAsset.from_bytes(path=path, content=content)

    def test_bytes_round_trip_and_canonical_identity_cover_every_asset(self):
        a, b = self.asset(), self.asset("data/another.bin")
        first = self.project(a, b)
        self.assertEqual(first.revision, self.project(b, a).revision)
        self.assertNotEqual(first.revision, self.project(a, self.asset(b.path, b"changed")).revision)
        restored = SourceProject.model_validate_json(first.to_json())
        self.assertEqual(restored.revision, first.revision)
        self.assertEqual({a.path: a.read_bytes() for a in restored.assets}, {a.path: a.read_bytes() for a in first.assets})
        self.assertEqual(a.read_bytes(), b"\0\xff\x80\n")
        empty = self.project()
        old = empty.model_dump(mode="json")
        self.assertNotIn("assets", old)
        self.assertEqual(empty.to_json(), canonical_json(old))
        self.assertEqual(self.asset(content=b"").read_bytes(), b"")

    def test_noncanonical_or_invalid_encoding_is_rejected(self):
        for content in ("/x==", "YQ", "YQ===", "YQ==\n", "_w==", "😀", "====", "YQ==Yg=="):
            with self.subTest(content=content), self.assertRaises(ValidationError):
                SourceAsset(path="data.bin", content_base64=content)

    def test_paths_and_cross_collection_collisions_are_rejected(self):
        for path in ("../asset", "/asset", "a//b", "a/./b", "a/../b", r"a\b", "a:b", ".env",
                     "chipload/x", "Chipload/x", "CHIPLOAD.py", "a/" * 8 + "x", "x\0", "x" * 201):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                self.asset(path)
        for paths in (("shop.py",), ("SHOP.py",), ("shop.py/file",), ("data", "data/file"),
                      ("Data/file", "data/FILE"), ("same", "same")):
            with self.subTest(paths=paths), self.assertRaises(ValidationError):
                self.project(*(self.asset(path) for path in paths))
        with self.assertRaises(ValidationError):
            self.project(self.asset("shop.py"), files=(SourceFile(path="other.py", content=""),))

    def test_decoded_serialized_and_combined_file_budgets_apply(self):
        large = self.asset(content=b"\0" * MAX_ASSET_BYTES)
        self.assertEqual(len(self.project(large).assets[0].read_bytes()), MAX_ASSET_BYTES)
        for size in (MAX_ASSET_BYTES + 1, MAX_ASSET_BYTES + 3):
            with self.subTest(size=size), self.assertRaises(ValidationError):
                SourceAsset(path="large", content_base64=base64.b64encode(b"\0" * size).decode())
        with self.assertRaises(ValueError):
            self.asset(content=b"\0" * (MAX_ASSET_BYTES + 1))
        with self.assertRaises(ValidationError):
            self.project(large, self.asset("other", b"\0" * MAX_ASSET_BYTES))
        self.project(*(self.asset(f"file-{i}") for i in range(127)))
        with self.assertRaises(ValidationError):
            self.project(*(self.asset(f"file-{i}") for i in range(128)))

    def test_materialization_preserves_exact_bytes_and_refuses_existing_paths(self):
        source = self.project(self.asset())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "work"
            work.mkdir()
            materialize(source, work)
            self.assertEqual((work / source.assets[0].path).read_bytes(), source.assets[0].read_bytes())
            self.assertEqual((work / "shop.py").read_text(), source.files[0].content)
            with self.assertRaises(ValueError):
                materialize(source, work)
            link = root / "linked"
            link.symlink_to(work, target_is_directory=True)
            with self.assertRaises(ValueError):
                materialize(source, link)
            empty = root / "empty"
            empty.mkdir()
            (empty / "data").symlink_to(work, target_is_directory=True)
            with self.assertRaises(ValueError):
                materialize(source, empty)


if __name__ == "__main__":
    unittest.main()
