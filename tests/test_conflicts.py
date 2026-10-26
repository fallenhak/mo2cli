import json
import struct
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from mo2cli import bsa, conflicts
from mo2cli.cli import main
from mo2cli.workspace import Instance, Mo2Error


def make_bsa(files: dict[str, list[str]], version: int = 105) -> bytes:
    """files maps a folder (backslashes) to file names."""
    folder_size = 16 if version == 104 else 24
    folders = list(files.items())
    file_names = b"".join(name.encode() + b"\0" for _, names in folders for name in names)
    count = sum(len(names) for _, names in folders)
    header = struct.pack("<4sIIIIIIIHH", b"BSA\0", version, 36, 3, len(folders), count, sum(len(f) + 1 for f, _ in folders), len(file_names), 0, 0)
    blocks = b""
    records = b""
    for folder, names in folders:
        name = folder.encode() + b"\0"
        records += struct.pack("<QII", 0, len(names), 0) if version == 104 else struct.pack("<QIIQ", 0, len(names), 0, 0)
        blocks += bytes([len(name)]) + name + b"".join(struct.pack("<QII", 0, 1, 0) for _ in names)
    return header + records + blocks + file_names + b"data"


class ConflictTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        mods = self.root / "mods"

        def put(mod, relative, data=b"x"):
            path = mods / mod / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        # modlist order: file order, first line wins
        put("Top", "textures/a.dds")
        put("Top", "scripts/s.pex")
        put("Mid", "textures/a.dds")
        put("Mid", "textures/b.dds")
        put("Low", "textures/a.dds")
        put("Low", "textures/b.dds")
        put("Hidden", "meshes/m.nif")
        put("Cover", "meshes/m.nif")
        put("Cover", "meshes/other.nif")
        put("Empty", "meta.ini", b"[General]")
        put("Disabled", "textures/a.dds")
        put("BsaMod", "Plug.esp")
        put("BsaMod", "Plug.bsa", make_bsa({"textures": ["a.dds", "bsaonly.dds"], "meshes": ["m.nif"]}))
        put("Plug2Mod", "Plug2.esp")
        put("Plug2Mod", "Plug2 - Textures.bsa", make_bsa({"textures": ["bsaonly.dds"]}, version=104))
        (mods / "Sep_separator").mkdir()
        (self.root / "profiles" / "Default").mkdir(parents=True)
        (self.root / "profiles" / "Default" / "modlist.txt").write_text(
            "+Top\n+Sep_separator\n+Mid\n+Low\n+Cover\n+Hidden\n-Disabled\n+Empty\n+BsaMod\n+Plug2Mod\n", encoding="utf-8")
        (self.root / "profiles" / "Default" / "plugins.txt").write_text("*Plug.esp\n*Plug2.esp\n", encoding="utf-8")
        (self.root / "profiles" / "Default" / "loadorder.txt").write_text("Plug.esp\nPlug2.esp\n", encoding="utf-8")
        (self.root / "ModOrganizer.ini").write_text("[General]\nselected_profile=Default\n", encoding="utf-8")
        self.instance = Instance.open(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def by_path(self, data):
        return {item["path"]: item for item in data["conflicts"]}

    def test_bsa_reader_both_versions(self):
        for version in (104, 105):
            path = self.root / f"t{version}.bsa"
            path.write_bytes(make_bsa({"textures\\Armor": ["A.dds", "B.dds"], "meshes": ["x.nif"]}, version))
            self.assertEqual(bsa.list_files(path), ["textures/armor/a.dds", "textures/armor/b.dds", "meshes/x.nif"])
        path.write_bytes(b"nope")
        with self.assertRaises(Mo2Error):
            bsa.list_files(path)

    def test_loose_conflicts_winner_and_losers(self):
        data = conflicts.report(self.instance, "Default")
        found = self.by_path(data)
        self.assertEqual(found["textures/a.dds"]["winner"], {"mod": "Top", "kind": "loose"})
        self.assertEqual([item["mod"] for item in found["textures/a.dds"]["losers"]], ["Mid", "Low"])
        self.assertEqual(found["textures/b.dds"]["winner"]["mod"], "Mid")
        self.assertNotIn("scripts/s.pex", found)
        self.assertNotIn("meta.ini", found)
        summary = {item["mod"]: item for item in data["mods"]}
        self.assertEqual(summary["Top"], {"mod": "Top", "overwrites": 1, "overwritten_by": 0})
        self.assertEqual(summary["Mid"]["overwrites"], 1)
        self.assertEqual(summary["Mid"]["overwritten_by"], 1)
        self.assertEqual(summary["Low"]["overwritten_by"], 2)

    def test_filters(self):
        self.assertEqual(list(self.by_path(conflicts.report(self.instance, "Default", extensions=".nif"))), ["meshes/m.nif"])
        self.assertEqual(list(self.by_path(conflicts.report(self.instance, "Default", extensions="nif,.pex"))), ["meshes/m.nif"])
        only = conflicts.report(self.instance, "Default", mod="hidden")
        self.assertEqual(list(self.by_path(only)), ["meshes/m.nif"])
        self.assertEqual([item["mod"] for item in only["mods"]], ["Hidden"])

    def test_include_bsa(self):
        plain = self.by_path(conflicts.report(self.instance, "Default"))
        self.assertNotIn("textures/bsaonly.dds", plain)
        data = self.by_path(conflicts.report(self.instance, "Default", include_bsa=True))
        # loose beats archived
        self.assertEqual(data["textures/a.dds"]["winner"]["kind"], "loose")
        self.assertIn({"mod": "BsaMod", "kind": "bsa", "archive": "Plug.bsa", "plugin": "Plug.esp"}, data["textures/a.dds"]["losers"])
        # later plugin's archive wins between archives
        self.assertEqual(data["textures/bsaonly.dds"]["winner"]["mod"], "Plug2Mod")
        self.assertEqual(data["textures/bsaonly.dds"]["losers"][0]["mod"], "BsaMod")

    def test_redundant(self):
        data = conflicts.redundant(self.instance, "Default")
        self.assertEqual([item["mod"] for item in data["empty"]], ["Empty"])
        names = {item["mod"]: item for item in data["fully_overwritten"]}
        self.assertEqual(set(names), {"Low", "Hidden"})
        self.assertEqual(names["Low"]["overwritten_by"], ["Mid", "Top"])
        self.assertNotIn("Sep_separator", json.dumps(data))

    def test_cli_json(self):
        out = StringIO()
        with redirect_stdout(out):
            code = main(["--instance", str(self.root), "--json", "conflicts", "report", "--ext", ".dds"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["count"], 2)
        out = StringIO()
        with redirect_stdout(out):
            main(["--instance", str(self.root), "--json", "conflicts", "redundant"])
        self.assertEqual(json.loads(out.getvalue())["empty"][0]["mod"], "Empty")


if __name__ == "__main__":
    unittest.main()
