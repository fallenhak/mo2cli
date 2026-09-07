import tempfile
import unittest
from pathlib import Path

from mo2cli.formats import ModEntry, ModList, PluginEntry, PluginList


class FormatTests(unittest.TestCase):
    def test_modlist_preserves_comments_and_changes_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "modlist.txt"
            path.write_text("# header\n-Disabled Mod\n+Enabled Mod\n*Foreign Mod\n", encoding="utf-8")
            model = ModList.read(path)
            model.set_enabled("Disabled Mod", True)
            self.assertEqual(model.entries[0], ModEntry("Disabled Mod", True, "+"))
            self.assertIn("# header\r\n+Disabled Mod", model.render())
            self.assertEqual(model.entries[2].foreign, True)

    def test_move_reorders_only_entries(self):
        model = ModList(["# header", ModEntry("A"), "", ModEntry("B")])
        model.move("B", 0)
        self.assertEqual([entry.name for entry in model.entries], ["B", "A"])
        self.assertEqual(model.render().splitlines(), ["# header", "+B", "", "+A"])

        model.move("B", 1)
        self.assertEqual([entry.name for entry in model.entries], ["A", "B"])

    def test_move_ui_uses_visible_mo2_order(self):
        model = ModList([ModEntry("A"), ModEntry("B"), ModEntry("C")])
        model.move_ui("A", 0)
        self.assertEqual([entry.name for entry in model.entries], ["B", "C", "A"])

    def test_plugins_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plugins.txt"
            path.write_text("# header\n*Skyrim.esm\nSome.esp\n", encoding="utf-8")
            model = PluginList.read(path)
            model.set_enabled("Some.esp", True)
        model.move("Some.esp", 0)
        self.assertEqual([entry.name for entry in model.entries], ["Some.esp", "Skyrim.esm"])
        self.assertEqual(model.entries[0], PluginEntry("Some.esp", True))

        model.move("Some.esp", 1)
        self.assertEqual([entry.name for entry in model.entries], ["Skyrim.esm", "Some.esp"])
