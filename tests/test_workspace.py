import configparser
import json
import tempfile
import unittest
from pathlib import Path

from mo2cli.workspace import Instance, Mo2Error
from mo2cli.cli import main


class WorkspaceTests(unittest.TestCase):
    def make_instance(self) -> Path:
        root = Path(self.temp.name)
        (root / "mods" / "Base Mod").mkdir(parents=True)
        (root / "mods" / "Base Mod" / "textures").mkdir()
        (root / "mods" / "Base Mod" / "textures" / "same.dds").write_bytes(b"base")
        (root / "mods" / "Patch Mod").mkdir()
        (root / "mods" / "Patch Mod" / "textures").mkdir()
        (root / "mods" / "Patch Mod" / "textures" / "same.dds").write_bytes(b"patch")
        (root / "mods" / "Patch Mod" / "meta.ini").write_text("[General]\n", encoding="utf-8")
        (root / "mods" / "Base Mod" / "meta.ini").write_text("[General]\n", encoding="utf-8")
        (root / "mods" / "Unlisted Mod").mkdir()
        (root / "overwrite" / "textures").mkdir(parents=True)
        (root / "overwrite" / "textures" / "same.dds").write_bytes(b"overwrite")
        (root / "profiles" / "Default").mkdir(parents=True)
        (root / "profiles" / "Default" / "modlist.txt").write_text("+Patch Mod\n+Base Mod\n", encoding="utf-8")
        (root / "profiles" / "Default" / "plugins.txt").write_text("*Skyrim.esm\nTest.esp\n", encoding="utf-8")
        (root / "profiles" / "Default" / "loadorder.txt").write_text("Test.esp\nSkyrim.esm\n", encoding="utf-8")
        (root / "ModOrganizer.ini").write_text(
            "[General]\nselected_profile=Default\ngameName=Skyrim Special Edition\n[Settings]\nbase_directory=%BASE_DIR%\n[customExecutables]\n1\\title=Test Tool\n1\\binary=C:/Windows/System32/cmd.exe\n1\\arguments=/c echo test\n1\\workingDirectory=C:/Windows\n",
            encoding="utf-8",
        )
        return root

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def test_reads_instance_and_selected_profile(self):
        instance = Instance.open(self.make_instance())
        self.assertEqual(instance.selected_profile, "Default")
        self.assertEqual(instance.game_name, "Skyrim Special Edition")
        self.assertEqual(instance.list_profiles(), ["Default"])
        self.assertEqual(instance.executables()[0]["title"], "Test Tool")

    def test_conflict_and_snapshot(self):
        instance = Instance.open(self.make_instance())
        conflicts = instance.conflicts("Default")
        self.assertEqual(conflicts[0]["path"], "textures/same.dds")
        self.assertFalse(any(item["path"] == "meta.ini" for item in conflicts))
        self.assertEqual(conflicts[0]["winner"], "overwrite")
        self.assertEqual(instance.file_owners("Default", "textures/same.dds")[0]["mod"], "overwrite")
        snapshot = json.loads(instance.json_snapshot("Default"))
        self.assertEqual(snapshot["profile"], "Default")
        self.assertEqual(len(snapshot["mods"]), 2)
        self.assertEqual(snapshot["loadorder"], ["Test.esp", "Skyrim.esm"])

        issue_codes = {issue["code"] for issue in instance.doctor("Default")}
        self.assertIn("unlisted-mod", issue_codes)

    def test_virtual_files_can_be_materialized(self):
        instance = Instance.open(self.make_instance())
        destination = Path(self.temp.name) / "materialized"
        result = instance.materialize("Default", destination)
        self.assertGreater(result["files"], 0)
        self.assertEqual((destination / "textures" / "same.dds").read_bytes(), b"overwrite")

    def test_plugin_move_writes_loadorder_not_plugins(self):
        root = self.make_instance()
        result = main(["plugins", "--instance", str(root), "--profile", "Default", "move", "Skyrim.esm", "0"])
        self.assertEqual(result, 0)
        self.assertEqual((root / "profiles" / "Default" / "loadorder.txt").read_text(), "Skyrim.esm\nTest.esp\n")
        self.assertEqual((root / "profiles" / "Default" / "plugins.txt").read_text(), "*Skyrim.esm\nTest.esp\n")

    def test_profile_lifecycle_and_selected_profile(self):
        instance = Instance.open(self.make_instance())
        instance.create_profile("Copy", "Default")
        self.assertEqual(instance.list_profiles(), ["Copy", "Default"])
        instance.write_selected_profile("Copy")
        self.assertEqual(Instance.open(instance.root).selected_profile, "Copy")
        instance.delete_profile("Default")
        with self.assertRaises(Mo2Error):
            instance.delete_profile("Copy")

    def test_instance_open_finds_parent_directory(self):
        root = self.make_instance()
        sub_dir = root / "mods" / "SomeMod"
        sub_dir.mkdir(parents=True, exist_ok=True)
        # Opening from sub_dir should automatically discover the root instance
        instance = Instance.open(sub_dir)
        self.assertEqual(instance.root.resolve(), root.resolve())

