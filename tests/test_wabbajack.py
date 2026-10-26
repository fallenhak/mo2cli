import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from mo2cli import wabbajack
from mo2cli.cli import main
from mo2cli.workspace import Instance, Mo2Error
from tests.test_stockgame import make_pe


class WabbajackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "inst"
        self.game = base / "game"
        (self.game / "Data").mkdir(parents=True)
        profile = self.root / "profiles" / "Default"
        profile.mkdir(parents=True)
        (self.root / "downloads").mkdir()
        for mod in ("ModA", "Pandora Output", "Disabled Output"):
            (self.root / "mods" / mod).mkdir(parents=True)
        (self.root / "mods" / "ModA" / "meta.ini").write_text("[General]\ninstallationFile=a.7z\n", encoding="utf-8")
        (profile / "modlist.txt").write_text("-Disabled Output\n+Pandora Output\n+ModA\n", encoding="utf-8")
        (profile / "plugins.txt").write_text("", encoding="utf-8")
        (self.root / "downloads" / "a.7z").write_bytes(b"a")
        (self.root / "downloads" / "a.7z.meta").write_text("[General]\ngameName=skyrimspecialedition\nmodID=1\nfileID=2\n", encoding="utf-8")
        (self.root / "ModOrganizer.ini").write_text(f"[General]\ngameName=Skyrim Special Edition\nselected_profile=Default\ngamePath=@ByteArray({self.game.as_posix()})\n", encoding="utf-8")
        self.instance = Instance.open(self.root)
        self.exe = base / "wabbajack-cli.exe"
        self.exe.write_bytes(b"MZ")

    def tearDown(self):
        self.temp.cleanup()

    def test_preflight_clean_except_untagged_output(self):
        result = wabbajack.preflight(self.instance)
        self.assertEqual([item["mod"] for item in result["generated_mods"]], ["Pandora Output"])
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["problems"]), 1)
        (self.root / "mods" / "Pandora Output" / "meta.ini").write_text("[General]\nnotes=WABBAJACK_NOMATCH_INCLUDE\n", encoding="utf-8")
        self.assertTrue(wabbajack.preflight(self.instance)["ok"])

    def test_preflight_settings_overwrite_downloads_and_stockgame(self):
        settings = self.root / "x.compiler_settings"
        settings.write_text(json.dumps({"NoMatchInclude": ["mods\\Pandora Output"]}), encoding="utf-8")
        (self.root / "overwrite").mkdir()
        (self.root / "overwrite" / "stray.txt").write_text("x", encoding="utf-8")
        (self.root / "downloads" / "b.7z").write_bytes(b"b")
        (self.root / "Stock Game").mkdir()
        (self.root / "Stock Game" / "SkyrimSE.exe").write_bytes(make_pe((1, 6, 1170, 0)))
        result = wabbajack.preflight(self.instance, settings=str(settings), expect_version="1.6.640.0")
        self.assertTrue(result["generated_mods"][0]["in_settings"])
        self.assertEqual(result["downloads"]["without_meta"], ["b.7z"])
        self.assertEqual(result["overwrite"]["sample"], ["stray.txt"])
        self.assertEqual(result["stock_game"]["version"], "1.6.1170.0")
        self.assertEqual(len(result["problems"]), 3)
        with self.assertRaises(Mo2Error):
            wabbajack.preflight(self.instance, settings=str(self.root / "missing.json"))

    def test_compile_command_blocks_and_renames(self):
        out = Path(self.temp.name) / "out" / "My List.wabbajack"
        plan = wabbajack.compile_modlist(self.instance, None, str(out), exe=str(self.exe), dry_run=True)
        self.assertEqual(plan["command"][1:], ["compile", "-i", str(self.root), "-o", str(out.parent)])
        with patch("mo2cli.wabbajack.subprocess.run", side_effect=AssertionError("blocked")):
            blocked = wabbajack.compile_modlist(self.instance, None, str(out), exe=str(self.exe))
        self.assertFalse(blocked["ok"])

        def fake_run(command, **kwargs):
            (out.parent / "Default.wabbajack").write_bytes(b"w")
            return subprocess.CompletedProcess(command, 0, "done", "")

        with patch("mo2cli.wabbajack.subprocess.run", side_effect=fake_run):
            result = wabbajack.compile_modlist(self.instance, None, str(out), exe=str(self.exe), force=True)
        self.assertTrue(result["ok"])
        self.assertTrue(out.is_file())
        self.assertFalse((out.parent / "Default.wabbajack").exists())
        with patch("mo2cli.wabbajack.subprocess.run", return_value=subprocess.CompletedProcess([], 3, "", "boom")):
            failed = wabbajack.compile_modlist(self.instance, None, str(out), exe=str(self.exe), force=True)
        self.assertFalse(failed["ok"])

    def test_cli_check_and_dry_run(self):
        out = StringIO()
        with redirect_stdout(out):
            code = main(["--instance", str(self.root), "--json", "wabbajack", "check"])
        self.assertEqual(code, 1)
        self.assertIn("problems", json.loads(out.getvalue()))
        out = StringIO()
        with redirect_stdout(out):
            code = main(["--instance", str(self.root), "--json", "wabbajack", "compile", "--output", str(Path(self.temp.name) / "o.wabbajack"), "--exe", str(self.exe), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out.getvalue())["dry_run"])
