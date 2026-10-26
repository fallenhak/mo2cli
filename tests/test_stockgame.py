import json
import struct
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from mo2cli import stockgame
from mo2cli.cli import main
from mo2cli.journal import undo_last
from mo2cli.workspace import Instance, Mo2Error


def make_pe(version=(1, 6, 1170, 0)) -> bytes:
    """Smallest PE32+ image that has a VS_VERSION_INFO resource."""
    ms = (version[0] << 16) | version[1]
    ls = (version[2] << 16) | version[3]
    fixed = struct.pack("<IIIIIIIIIIIII", 0xFEEF04BD, 0x10000, ms, ls, ms, ls, 0x3F, 0, 0x40004, 1, 0, 0, 0)
    info = struct.pack("<HHH", 6 + 34 + len(fixed), len(fixed), 0) + "VS_VERSION_INFO".encode("utf-16-le") + b"\0\0\0\0\0\0"
    block = info + b"\0" * (6 + 34 - len(info)) + fixed if len(info) < 40 else info + fixed
    # resource tree: root(type 16) -> name dir -> language dir -> data entry -> data
    root = struct.pack("<IIHHHH", 0, 0, 0, 0, 0, 1) + struct.pack("<II", 16, 0x80000000 | 0x18)
    names = struct.pack("<IIHHHH", 0, 0, 0, 0, 0, 1) + struct.pack("<II", 1, 0x80000000 | 0x30)
    languages = struct.pack("<IIHHHH", 0, 0, 0, 0, 0, 1) + struct.pack("<II", 1033, 0x48)
    entry = struct.pack("<IIII", 0x1000 + 0x58, len(block), 0, 0)
    resource = root + names + languages + entry + block
    optional_size = 112 + 16 * 8
    header = bytearray(0x40)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 0x3C, 0x40)
    coff = struct.pack("<HHIIIHH", 0x8664, 1, 0, 0, 0, optional_size, 0x22)
    optional = bytearray(optional_size)
    struct.pack_into("<H", optional, 0, 0x20B)
    struct.pack_into("<II", optional, 112 + 2 * 8, 0x1000, len(resource))
    section = b".rsrc\0\0\0" + struct.pack("<IIIIIIHHI", len(resource), 0x1000, len(resource), 0x200, 0, 0, 0, 0, 0x40000040)
    image = bytes(header) + b"PE\0\0" + coff + bytes(optional) + section
    return image.ljust(0x200, b"\0") + resource


class StockGameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "inst"
        (self.root / "profiles" / "Default").mkdir(parents=True)
        (self.root / "mods").mkdir()
        (self.root / "ModOrganizer.ini").write_text("[General]\nselected_profile=Default\n", encoding="utf-8")
        self.instance = Instance.open(self.root)
        self.steam = base / "steam"
        (self.steam / "Data" / "Video").mkdir(parents=True)
        (self.steam / "Data" / "Interface").mkdir()
        (self.steam / "Papyrus Compiler").mkdir()
        (self.steam / "SkyrimSE.exe").write_bytes(make_pe())
        for name in ["steam_api64.dll", "skse64_loader.exe", "d3d11.dll", "enblocal.ini", "Papyrus Compiler/x.exe"]:
            (self.steam / name).write_bytes(b"x")
        for name in ["Skyrim.esm", "Update.esm", "Skyrim - Textures0.bsa", "ccBGSSSE001-Fish.esm", "ccBGSSSE001-Fish.bsa", "Video/intro.bik", "MyMod.esp", "mod.bsa", "Interface/loose.txt", "SKSE"]:
            target = self.steam / "Data" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"d")

    def tearDown(self):
        self.temp.cleanup()

    def test_pe_version_reader(self):
        path = Path(self.temp.name) / "t.exe"
        path.write_bytes(make_pe((1, 7, 104, 0)))
        self.assertEqual(stockgame.pe_file_version(path), "1.7.104.0")
        path.write_bytes(b"not a pe")
        self.assertIsNone(stockgame.pe_file_version(path))

    def test_xxh64_vectors(self):
        def digest(data):
            engine = stockgame.XXH64()
            engine.update(data)
            return engine.intdigest()

        self.assertEqual(digest(b""), 0xEF46DB3751D8E999)
        self.assertEqual(digest(b"abc"), 0x44BC2CF5AD770999)
        chunked = stockgame.XXH64()
        for index in range(0, 300, 7):
            chunked.update(bytes(range(256))[index % 200 : index % 200 + 7])
        whole = stockgame.XXH64()
        whole.update(b"".join(bytes(range(256))[index % 200 : index % 200 + 7] for index in range(0, 300, 7)))
        self.assertEqual(chunked.intdigest(), whole.intdigest())

    def test_plan_copies_only_vanilla_data(self):
        copies, skipped = stockgame.plan_copy(self.steam)
        names = {str(relative).replace("\\", "/") for _, relative in copies}
        self.assertIn("SkyrimSE.exe", names)
        self.assertIn("steam_api64.dll", names)
        self.assertIn("Papyrus Compiler/x.exe", names)
        for vanilla in ["Data/Skyrim.esm", "Data/Update.esm", "Data/Skyrim - Textures0.bsa", "Data/ccBGSSSE001-Fish.esm", "Data/ccBGSSSE001-Fish.bsa", "Data/Video/intro.bik"]:
            self.assertIn(vanilla, names)
        for mod in ["skse64_loader.exe", "d3d11.dll", "enblocal.ini", "Data/MyMod.esp", "Data/mod.bsa", "Data/Interface/loose.txt", "Data/SKSE"]:
            self.assertNotIn(mod, names)
        self.assertIn("Data/MyMod.esp", skipped)
        self.assertIn("skse64_loader.exe", skipped)

    def test_create_dry_run_then_real_then_undo(self):
        target = self.root / "Stock Game"
        plan = stockgame.create(self.instance, self.steam, target, "1.6.1170.0", dry_run=True)
        self.assertFalse(target.exists())
        self.assertGreater(plan["to_copy"], 5)
        result = stockgame.create(self.instance, self.steam, target, "1.6.1170.0")
        self.assertTrue((target / "Data" / "Skyrim.esm").is_file())
        self.assertFalse((target / "Data" / "MyMod.esp").exists())
        self.assertTrue(result["verify"]["ok"])
        self.assertTrue((self.steam / "Data" / "MyMod.esp").is_file())
        again = stockgame.create(self.instance, self.steam, target)
        self.assertEqual(again["to_copy"], 0)
        undo_last(self.instance)  # second run was journaled but not reversible; first one is undone
        self.assertFalse(target.exists())

    def test_version_mismatch_blocks_unless_allowed(self):
        with self.assertRaises(Mo2Error):
            stockgame.create(self.instance, self.steam, self.root / "Stock Game", "1.5.97.0")
        self.assertFalse((self.root / "Stock Game").exists())
        result = stockgame.create(self.instance, self.steam, self.root / "Stock Game", "1.5.97.0", allow_mismatch=True)
        self.assertTrue(result["version_mismatch_allowed"])

    def test_destination_inside_source_is_refused(self):
        with self.assertRaises(Mo2Error):
            stockgame.create(self.instance, self.steam, self.steam / "Stock Game")

    def test_verify_cli(self):
        copy = self.root / "Stock Game"
        copy.mkdir()
        (copy / "SkyrimSE.exe").write_bytes(make_pe())
        out = StringIO()
        with redirect_stdout(out):
            code = main(["--instance", str(self.root), "--json", "stockgame", "verify", "--expect-version", "1.6.1170.0"])
        data = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(data["version"], "1.6.1170.0")
        self.assertEqual(len(data["sha256"]), 64)
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["--instance", str(self.root), "stockgame", "verify", "--expect-version", "1.7.104.0"]), 1)

    def test_create_cli_dry_run_needs_no_lock(self):
        with patch("mo2cli.safety._running_process_names", return_value={"modorganizer.exe"}):
            with redirect_stdout(StringIO()):
                code = main(["--instance", str(self.root), "stockgame", "create", "--from", str(self.steam), "--dry-run"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
