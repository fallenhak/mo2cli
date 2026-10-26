import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from mo2cli import manifest
from mo2cli.cli import main
from mo2cli.journal import undo_last
from mo2cli.separators import get_color, set_color
from mo2cli.workspace import Instance, Mo2Error


def make_instance(base: Path, name: str, modlist: str, settings: str) -> Instance:
    root = base / name
    profile = root / "profiles" / "Default"
    profile.mkdir(parents=True)
    (profile / "modlist.txt").write_text(modlist, encoding="utf-8")
    (profile / "plugins.txt").write_text("", encoding="utf-8")
    (root / "downloads").mkdir()
    (root / "ModOrganizer.ini").write_text(
        f"[General]\ngameName=Skyrim Special Edition\nselected_profile=Default\n\n[Settings]\n{settings}", encoding="utf-8"
    )
    for mod in ("ModA", "ModB", "ModC"):
        (root / "mods" / mod).mkdir(parents=True)
    return Instance.open(root)


class ManifestExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.a = make_instance(self.base, "A", "+ModB\n+ModA\n-ModC\n+Visuals_separator\n+Gameplay_separator\n", "check_for_updates=false\ncompact_downloads=true\n")
        (self.a.mods_dir / "Visuals_separator").mkdir()
        (self.a.mods_dir / "Gameplay_separator").mkdir()
        set_color(self.a.mods_dir / "Visuals_separator", "#336699")
        (self.a.mods_dir / "ModA" / "meta.ini").write_text("[General]\nmodid=100\nfileid=200\nversion=1.2\ninstallationFile=ModA-1.2.7z\n", encoding="utf-8")
        (self.a.mods_dir / "ModC" / "meta.ini").write_text("[General]\nmanualURL=https://example.test/c\n", encoding="utf-8")
        (self.a.downloads_dir / "ModA-1.2.7z").write_bytes(b"x")
        self.out = self.base / "manifest.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_export_contents(self):
        summary = manifest.export(self.a, self.out)
        data = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(summary["mods"], 3)
        self.assertEqual(summary["source_missing"], ["ModB", "ModC"])
        by_name = {item["name"]: item for item in data["mods"]}
        self.assertEqual(by_name["ModA"]["nexus"], {"game": "skyrimspecialedition", "mod_id": 100, "file_id": 200})
        self.assertEqual(by_name["ModA"]["separator"], "Visuals")
        self.assertTrue(by_name["ModC"]["disabled"])
        self.assertEqual(by_name["ModC"]["manual_url"], "https://example.test/c")
        self.assertNotIn("path", by_name["ModA"])
        self.assertEqual([item["name"] for item in data["separators"]], ["Visuals", "Gameplay"])
        self.assertEqual(data["separators"][0]["color"], "#336699")
        self.assertEqual([item["name"] for item in data["order"]], ["ModB", "ModA", "ModC", "Visuals_separator", "Gameplay_separator"])
        self.assertEqual(data["settings"]["Settings"]["compact_downloads"], "true")
        with_paths = manifest.export(self.a, self.out, with_paths=True)
        self.assertEqual(with_paths["source_missing"], ["ModB", "ModC"])
        self.assertTrue(json.loads(self.out.read_text(encoding="utf-8"))["mods"][1]["path"].endswith("ModA-1.2.7z"))

    def test_round_trip_and_undo(self):
        manifest.export(self.a, self.out)
        b = make_instance(self.base, "B", "+ModA\n+ModB\n+ModC\n", "check_for_updates=true\n")
        modlist = b.profiles_dir / "Default" / "modlist.txt"
        before = modlist.read_text(encoding="utf-8")
        plan = manifest.apply(b, self.out, dry_run=True)
        self.assertEqual(modlist.read_text(encoding="utf-8"), before)
        self.assertEqual(plan["extras"]["order"], 5)
        self.assertEqual(len(plan["skipped"]), 3)
        result = manifest.apply(b, self.out)
        self.assertEqual(result["errors"], [])
        self.assertEqual([item["name"] for item in result["skipped"]], ["ModB", "ModA", "ModC"])
        text = modlist.read_text(encoding="utf-8")
        self.assertEqual([line for line in text.splitlines() if line and not line.startswith("#")], ["+ModB", "+ModA", "-ModC", "+Visuals_separator", "+Gameplay_separator"])
        self.assertEqual(get_color(b.mods_dir / "Visuals_separator"), "#336699")
        ini = b.ini.read_text(encoding="utf-8")
        self.assertIn("check_for_updates=false", ini)
        self.assertIn("compact_downloads=true", ini)
        # Applying again changes nothing.
        again = manifest.apply(b, self.out)
        self.assertEqual(again["extras"]["settings_changed"], [])
        self.assertEqual(again["extras"]["separator_colors"], [])
        # Undo walks back through settings, order/colors and separators.
        for _ in range(8):
            try:
                undo_last(b)
            except Mo2Error:
                break
        self.assertEqual(modlist.read_text(encoding="utf-8"), before)

    def test_apply_rejects_bad_sections(self):
        b = make_instance(self.base, "B", "+ModA\n", "")
        bad = self.base / "bad.json"
        bad.write_text(json.dumps({"order": "nope"}), encoding="utf-8")
        with self.assertRaises(Mo2Error):
            manifest.apply(b, bad, dry_run=True)

    def test_cli_export(self):
        out = StringIO()
        with redirect_stdout(out):
            code = main(["--instance", str(self.a.root), "--json", "manifest", "export", str(self.out)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["mods"], 3)
        self.assertTrue(self.out.is_file())
