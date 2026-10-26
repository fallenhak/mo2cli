import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from mo2cli.cli import main
from mo2cli.journal import undo_last
from mo2cli.settings import get_setting, list_settings, set_setting
from mo2cli.workspace import Instance, Mo2Error

INI = (
    "[General]\r\nselected_profile=Default\r\ngamePath=@ByteArray(C:\\\\Games\\\\Skyrim)\r\n; keep me\r\n\r\n"
    "[Settings]\r\nbase_directory=%BASE_DIR%\r\n\r\n[Servers]\r\nNexus\\premium=true"
)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "profiles" / "Default").mkdir(parents=True)
        (self.root / "mods").mkdir()
        self.ini = self.root / "ModOrganizer.ini"
        self.ini.write_bytes(INI.encode())
        self.instance = Instance.open(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def text(self):
        return self.ini.read_bytes().decode()

    def test_get_decodes_and_raw(self):
        self.assertEqual(get_setting(self.instance, "General", "gamePath")["value"], "C:\\Games\\Skyrim")
        self.assertEqual(get_setting(self.instance, "General", "gamePath", raw=True)["value"], "@ByteArray(C:\\\\Games\\\\Skyrim)")
        self.assertEqual(get_setting(self.instance, "Servers", "Nexus\\premium")["value"], "true")
        with self.assertRaises(Mo2Error):
            get_setting(self.instance, "General", "nope")

    def test_list(self):
        self.assertEqual([item["section"] for item in list_settings(self.instance)], ["General", "Settings", "Servers"])
        keys = [item["key"] for item in list_settings(self.instance, "General")]
        self.assertEqual(keys, ["selected_profile", "gamePath"])

    def test_set_existing_changes_one_line_only(self):
        set_setting(self.instance, "General", "selected_profile", "Other")
        self.assertEqual(self.text(), INI.replace("selected_profile=Default", "selected_profile=Other"))

    def test_set_encodes_and_raw(self):
        set_setting(self.instance, "Settings", "mod_directory", "C:\\Mods;x")
        self.assertIn('mod_directory="C:\\\\Mods;x"\r\n', self.text())
        set_setting(self.instance, "Settings", "mod_directory", "C:\\Mods", raw=True)
        self.assertIn("mod_directory=C:\\Mods\r\n", self.text())
        set_setting(self.instance, "Settings", "gamePath2", "C:\\G", as_bytearray=True)
        self.assertEqual(get_setting(self.instance, "Settings", "gamePath2")["value"], "C:\\G")

    def test_set_new_key_and_new_section_and_last_line_without_newline(self):
        set_setting(self.instance, "General", "extra", "1")
        self.assertIn("; keep me\r\nextra=1\r\n\r\n[Settings]", self.text())
        set_setting(self.instance, "Servers", "other", "2")
        self.assertTrue(self.text().endswith("Nexus\\premium=true\r\nother=2\r\n"))
        set_setting(self.instance, "Brand New", "k", "v")
        self.assertTrue(self.text().endswith("\r\n\r\n[Brand New]\r\nk=v\r\n"))

    def test_dry_run_and_noop_do_not_write_or_journal(self):
        before = self.ini.read_bytes()
        self.assertTrue(set_setting(self.instance, "General", "selected_profile", "X", dry_run=True)["changed"])
        self.assertEqual(self.ini.read_bytes(), before)
        result = set_setting(self.instance, "General", "selected_profile", "Default")
        self.assertFalse(result["changed"])
        self.assertNotIn("journal_id", result)

    def test_undo_restores_exact_bytes(self):
        before = self.ini.read_bytes()
        set_setting(self.instance, "General", "selected_profile", "Other")
        set_setting(self.instance, "Brand New", "k", "v")
        undo_last(self.instance)
        undo_last(self.instance)
        self.assertEqual(self.ini.read_bytes(), before)

    def test_cli_json(self):
        with patch("mo2cli.safety._running_process_names", return_value=set()):
            out = StringIO()
            with redirect_stdout(out):
                code = main(["--instance", str(self.root), "--json", "settings", "set", "Settings", "x", "1"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out.getvalue())["key"], "x")
            out = StringIO()
            with redirect_stdout(out):
                main(["--instance", str(self.root), "--json", "settings", "get", "Settings", "x"])
            self.assertEqual(json.loads(out.getvalue())["value"], "1")


if __name__ == "__main__":
    unittest.main()
