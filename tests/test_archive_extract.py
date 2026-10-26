import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

import py7zr

from mo2cli.archives import archive_common_root, extract_to_directory
from mo2cli.cli import main
from mo2cli.workspace import Mo2Error


def make_zip(path: Path, members: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as output:
        for name, data in members.items():
            output.writestr(name, data)
    return path


class ArchiveExtractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_extracts_zip_and_reports_files(self):
        archive = make_zip(self.root / "a.zip", {"x/one.txt": b"1", "two.txt": b"2"})
        result = extract_to_directory(archive, self.root / "out")
        self.assertEqual(result["files"], 2)
        self.assertEqual((self.root / "out" / "x" / "one.txt").read_bytes(), b"1")
        self.assertIsNone(result["strip_root"])

    def test_strip_root_removes_single_wrapper_folder(self):
        archive = make_zip(self.root / "a.zip", {"MO2/ModOrganizer.exe": b"exe", "MO2/plugins/p.py": b"p"})
        result = extract_to_directory(archive, self.root / "out", strip_root=True)
        self.assertEqual(result["strip_root"], "MO2")
        self.assertTrue((self.root / "out" / "ModOrganizer.exe").is_file())
        self.assertTrue((self.root / "out" / "plugins" / "p.py").is_file())
        self.assertFalse((self.root / "out" / "MO2").exists())

    def test_strip_root_refuses_archive_without_single_root(self):
        archive = make_zip(self.root / "a.zip", {"one.txt": b"1", "dir/two.txt": b"2"})
        with self.assertRaises(Mo2Error):
            extract_to_directory(archive, self.root / "out", strip_root=True)
        self.assertFalse((self.root / "out").exists())

    def test_zip_slip_member_is_rejected_and_nothing_is_written(self):
        archive = make_zip(self.root / "evil.zip", {"ok.txt": b"ok", "../escaped.txt": b"bad"})
        with self.assertRaises(Mo2Error):
            extract_to_directory(archive, self.root / "out")
        self.assertFalse((self.root / "escaped.txt").exists())
        self.assertFalse((self.root / "out").exists())

    def test_zip_slip_is_rejected_in_dry_run_too(self):
        archive = make_zip(self.root / "evil.zip", {"..\\escaped.txt": b"bad"})
        with self.assertRaises(Mo2Error):
            extract_to_directory(archive, self.root / "out", dry_run=True)

    def test_non_empty_destination_needs_replace_and_keeps_other_files(self):
        archive = make_zip(self.root / "a.zip", {"one.txt": b"new"})
        out = self.root / "out"
        out.mkdir()
        (out / "one.txt").write_bytes(b"old")
        (out / "keep.txt").write_bytes(b"keep")
        with self.assertRaises(Mo2Error):
            extract_to_directory(archive, out)
        self.assertEqual((out / "one.txt").read_bytes(), b"old")
        result = extract_to_directory(archive, out, replace=True)
        self.assertTrue(result["overwrite_existing"])
        self.assertEqual((out / "one.txt").read_bytes(), b"new")
        self.assertEqual((out / "keep.txt").read_bytes(), b"keep")

    def test_dry_run_writes_nothing(self):
        archive = make_zip(self.root / "a.zip", {"MO2/a.txt": b"1", "MO2/b.txt": b"2"})
        result = extract_to_directory(archive, self.root / "out", strip_root=True, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["files"], 2)
        self.assertEqual(result["sample"], ["a.txt", "b.txt"])
        self.assertFalse((self.root / "out").exists())

    def test_require_missing_file_aborts_before_touching_destination(self):
        archive = make_zip(self.root / "a.zip", {"a.txt": b"1"})
        with self.assertRaises(Mo2Error):
            extract_to_directory(archive, self.root / "out", require="ModOrganizer.exe")
        self.assertFalse((self.root / "out").exists())

    def test_extracts_7z(self):
        source = self.root / "src"
        (source / "root").mkdir(parents=True)
        (source / "root" / "f.txt").write_bytes(b"seven")
        archive = self.root / "a.7z"
        with py7zr.SevenZipFile(archive, "w") as output:
            output.write(source / "root" / "f.txt", "root/f.txt")
        extract_to_directory(archive, self.root / "out", strip_root=True)
        self.assertEqual((self.root / "out" / "f.txt").read_bytes(), b"seven")

    def test_common_root_detection(self):
        self.assertEqual(archive_common_root([{"name": "A/", "directory": True}, {"name": "A/b", "directory": False}]), "A")
        self.assertIsNone(archive_common_root([{"name": "A/b", "directory": False}, {"name": "C/d", "directory": False}]))
        self.assertIsNone(archive_common_root([{"name": "f", "directory": False}]))

    def test_cli_extract_json_and_missing_to(self):
        archive = make_zip(self.root / "a.zip", {"w/a.txt": b"1"})
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["archive", "extract", str(archive), "--to", str(self.root / "out"), "--strip-root", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buffer.getvalue())["files"], 1)
        self.assertTrue((self.root / "out" / "a.txt").is_file())
        self.assertEqual(main(["archive", "extract", str(archive)]), 2)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["archive", "extract", str(archive), "--to", str(self.root / "o2"), "--dry-run"]), 0)
        self.assertFalse((self.root / "o2").exists())


if __name__ == "__main__":
    unittest.main()
