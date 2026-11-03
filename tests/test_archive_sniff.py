import tempfile
import unittest
import zipfile
from pathlib import Path

from mo2cli.archives import _format_name, list_archive


class ArchiveSniffTests(unittest.TestCase):
    def test_signature_overrides_misleading_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            rar = Path(tmp) / "Mod.zip"
            rar.write_bytes(b"Rar!\x1a\x07\x01\x00" + b"\x00" * 16)
            self.assertEqual(_format_name(rar), "mod.zip.rar")
            seven = Path(tmp) / "Mod.7z"
            seven.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 16)
            self.assertEqual(_format_name(seven), "mod.7z")
            real = Path(tmp) / "Real.rar"
            with zipfile.ZipFile(real, "w") as archive:
                archive.writestr("a/b.txt", "x")
            self.assertEqual(_format_name(real), "real.rar.zip")
            self.assertEqual([item["name"] for item in list_archive(real)], ["a/b.txt"])


if __name__ == "__main__":
    unittest.main()
