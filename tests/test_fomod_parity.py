import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from mo2cli.fomod import compare_fomod_plus, inspect_archive, plan_archive, reconcile_selections, selections_from_fomod_plus
from mo2cli.workspace import Instance, Mo2Error


class FomodParityTests(unittest.TestCase):
    def setUp(self):
        from test_workspace import WorkspaceTests

        self.fixture = WorkspaceTests()
        self.fixture.setUp()
        self.root = self.fixture.make_instance()
        self.instance = Instance.open(self.root)

    def tearDown(self):
        self.fixture.tearDown()

    def make_archive(self, config: str, files: dict[str, bytes] | None = None) -> Path:
        descriptor, name = tempfile.mkstemp(suffix=".zip")
        os.close(descriptor)
        archive = Path(name)
        self.addCleanup(archive.unlink, missing_ok=True)
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("fomod/ModuleConfig.xml", config.encode("utf-8"))
            for name, content in (files or {}).items():
                output.writestr(name, content)
        return archive

    def test_instance_resolves_game_active_and_inactive_file_states(self):
        game = self.root / "Game"
        (game / "Data").mkdir(parents=True)
        (game / "Data" / "Dawnguard.esm").write_bytes(b"master")
        disabled = self.root / "mods" / "Disabled Mod"
        disabled.mkdir()
        (disabled / "DisabledPatch.esp").write_bytes(b"plugin")
        (self.root / "mods" / "Patch Mod" / "Test.esp").write_bytes(b"plugin")
        profile = self.root / "profiles" / "Default"
        (profile / "modlist.txt").write_text("+Patch Mod\n-Disabled Mod\n+Base Mod\n", encoding="utf-8")
        (profile / "loadorder.txt").write_text("Dawnguard.esm\nTest.esp\nSkyrim.esm\n", encoding="utf-8")
        ini = self.root / "ModOrganizer.ini"
        ini.write_text(ini.read_text(encoding="utf-8").replace(
            "gameName=Skyrim Special Edition\n",
            f"gameName=Skyrim Special Edition\ngamePath={game}\n",
        ), encoding="utf-8")

        instance = Instance.open(self.root)
        states = instance.fomod_file_states("Default", {
            "Dawnguard.esm", "textures/same.dds", "Test.esp", "DisabledPatch.esp", "Missing.esp",
        })

        self.assertEqual(states["dawnguard.esm"], "Active")
        self.assertEqual(states["textures/same.dds"], "Active")
        self.assertEqual(states["test.esp"], "Inactive")
        self.assertEqual(states["disabledpatch.esp"], "Inactive")
        self.assertEqual(states["missing.esp"], "Missing")

    def test_contextual_inspection_and_unavailable_selection_failure(self):
        config = '''<config>
  <moduleName>Context Test</moduleName>
  <installSteps order="Explicit"><installStep name="Main"><optionalFileGroups order="Explicit">
    <group name="Patch" type="SelectAtMostOne"><plugins order="Explicit">
      <plugin name="Dawnguard Patch"><description>Needs Dawnguard.</description><image path="img/patch.png" />
        <typeDescriptor><dependencyType><defaultType name="NotUsable" /><patterns><pattern>
          <dependencies><fileDependency file="Dawnguard.esm" state="Active" /></dependencies><type name="Optional" />
        </pattern></patterns></dependencyType></typeDescriptor>
        <files><folder source="Patch" destination="" /></files>
      </plugin>
    </plugins></group>
  </optionalFileGroups></installStep></installSteps>
</config>'''
        archive = self.make_archive(config, {"Patch/file.txt": b"patch"})

        unavailable = plan_archive(archive, {"Main/Patch": ["Dawnguard Patch"]}, file_states={"Dawnguard.esm": "Missing"})
        self.assertTrue(any("unavailable selection" in error for error in unavailable["errors"]))
        option = unavailable["steps"][0]["groups"][0]["options"][0]
        self.assertFalse(option["available"])
        self.assertEqual(option["type"], "NotUsable")
        self.assertEqual(option["image"], "img/patch.png")
        self.assertTrue(option["files"][0]["folder"])

        available = plan_archive(archive, {"Main/Patch": ["Dawnguard Patch"]}, file_states={"Dawnguard.esm": "Active"})
        self.assertEqual(available["errors"], [])
        self.assertEqual(available["selected"], ["Dawnguard Patch"])

    def test_duplicate_group_keys_are_exposed_and_reconciled(self):
        config = '''<config><installSteps order="Explicit"><installStep name="Step"><optionalFileGroups order="Explicit">
  <group name="Choice" type="SelectExactlyOne"><plugins><plugin name="First"><typeDescriptor><type name="Optional" /></typeDescriptor><files><file source="first.txt" /></files></plugin></plugins></group>
  <group name="Choice" type="SelectExactlyOne"><plugins><plugin name="Second"><typeDescriptor><type name="Optional" /></typeDescriptor><files><file source="second.txt" /></files></plugin></plugins></group>
</optionalFileGroups></installStep></installSteps></config>'''
        archive = self.make_archive(config, {"first.txt": b"1", "second.txt": b"2"})
        selections = {"Step/Choice": ["First"], "Step/Choice#2": ["Second"]}

        plan = plan_archive(archive, selections)
        keys = [group["key"] for group in plan["steps"][0]["groups"]]
        self.assertEqual(keys, ["Step/Choice", "Step/Choice#2"])
        self.assertEqual(plan["errors"], [])
        self.assertEqual(set(plan["selected"]), {"First", "Second"})

        from mo2cli.archives import extract_to_temp
        extracted = extract_to_temp(archive)
        try:
            reconciled, dropped = reconcile_selections(extracted.root, selections)
        finally:
            extracted.close()
        self.assertEqual(reconciled, selections)
        self.assertEqual(dropped, [])

        record = {
            "displayName": "Duplicate Test",
            "modId": 1,
            "options": [
                {"step": "Step", "group": "Choice", "name": "First", "selectionState": "Selected"},
                {"step": "Step", "group": "Choice", "name": "Second", "selectionState": "Selected"},
            ],
        }
        extracted = extract_to_temp(archive)
        try:
            self.assertEqual(selections_from_fomod_plus(extracted.root, record), selections)
        finally:
            extracted.close()
        compared = inspect_archive(archive, fomod_plus_record=record)
        self.assertTrue(compared["fomod_plus"]["matches"])

    def test_required_and_conditional_file_attributes_are_honored(self):
        config = '''<config><installSteps><installStep name="Main"><optionalFileGroups>
  <group name="Files" type="SelectAny"><plugins>
    <plugin name="Required"><typeDescriptor><type name="Required" /></typeDescriptor><files><file source="required.txt" /></files></plugin>
    <plugin name="Optional"><typeDescriptor><type name="Optional" /></typeDescriptor><files>
      <file source="always.txt" alwaysInstall="true" />
      <file source="usable.txt" installIfUsable="true" />
      <file source="selected.txt" />
    </files></plugin>
  </plugins></group>
</optionalFileGroups></installStep></installSteps></config>'''
        archive = self.make_archive(config, {
            "required.txt": b"required", "always.txt": b"always", "usable.txt": b"usable", "selected.txt": b"selected",
        })

        plan = plan_archive(archive, {"Main/Files": []})

        self.assertEqual(plan["errors"], [])
        self.assertEqual(plan["selected"], ["Required"])
        sources = {operation["source"] for operation in plan["operations"]}
        self.assertEqual(sources, {"required.txt", "always.txt", "usable.txt"})

    def test_select_all_cannot_be_cleared_explicitly(self):
        config = '''<config><installSteps><installStep name="Main"><optionalFileGroups>
  <group name="Everything" type="SelectAll"><plugins>
    <plugin name="One"><typeDescriptor><type name="Optional" /></typeDescriptor><files><file source="one.txt" /></files></plugin>
    <plugin name="Two"><typeDescriptor><type name="Optional" /></typeDescriptor><files><file source="two.txt" /></files></plugin>
  </plugins></group>
</optionalFileGroups></installStep></installSteps></config>'''
        archive = self.make_archive(config, {"one.txt": b"one", "two.txt": b"two"})

        plan = plan_archive(archive, {"Main/Everything": []})

        self.assertEqual(plan["errors"], [])
        self.assertEqual(set(plan["selected"]), {"One", "Two"})

    def test_fomod_plus_hidden_selected_records_are_reported_but_not_compared(self):
        config = '''<config><installSteps order="Explicit">
  <installStep name="Hidden"><visible><flagDependency flag="show" value="yes" /></visible><optionalFileGroups>
    <group name="Choice" type="SelectExactlyOne"><plugins><plugin name="Hidden Pick"><typeDescriptor><type name="Optional" /></typeDescriptor><files><file source="hidden.txt" /></files></plugin></plugins></group>
  </optionalFileGroups></installStep>
  <installStep name="Visible"><optionalFileGroups><group name="Choice" type="SelectExactlyOne"><plugins>
    <plugin name="Visible Pick"><typeDescriptor><type name="Optional" /></typeDescriptor><files><file source="visible.txt" /></files></plugin>
  </plugins></group></optionalFileGroups></installStep>
</installSteps></config>'''
        archive = self.make_archive(config, {"hidden.txt": b"hidden", "visible.txt": b"visible"})
        record = {
            "displayName": "Visibility Test",
            "modId": 2,
            "options": [
                {"step": "Hidden", "group": "Choice", "name": "Hidden Pick", "selectionState": "Selected"},
                {"step": "Visible", "group": "Choice", "name": "Visible Pick", "selectionState": "Selected"},
            ],
        }

        inspected = inspect_archive(archive, fomod_plus_record=record)

        self.assertTrue(inspected["fomod_plus"]["matches"])
        self.assertEqual(inspected["fomod_plus"]["planned"], ["Visible Pick"])
        self.assertEqual(inspected["fomod_plus"]["ignored_hidden"], ["Hidden Pick"])

    def test_fomod_plus_comparison_uses_group_identity_not_only_option_name(self):
        config = '''<config><installSteps><installStep name="Main"><optionalFileGroups order="Explicit">
  <group name="First" type="SelectAny"><plugins><plugin name="Shared"><typeDescriptor><type name="Optional" /></typeDescriptor><files><file source="first.txt" /></files></plugin></plugins></group>
  <group name="Second" type="SelectAny"><plugins><plugin name="Shared"><typeDescriptor><type name="Optional" /></typeDescriptor><files><file source="second.txt" /></files></plugin></plugins></group>
</optionalFileGroups></installStep></installSteps></config>'''
        archive = self.make_archive(config, {"first.txt": b"first", "second.txt": b"second"})
        plan = plan_archive(archive, {"Main/First": ["Shared"], "Main/Second": []})
        record = {
            "displayName": "Identity Test",
            "modId": 3,
            "options": [
                {"step": "Main", "group": "Second", "name": "Shared", "selectionState": "Selected"},
            ],
        }

        comparison = compare_fomod_plus(plan, record)

        self.assertFalse(comparison["matches"])
        self.assertEqual(comparison["missing"], ["Main/Second/Shared"])
        self.assertEqual(comparison["unexpected"], ["Main/First/Shared"])

    def test_versions_module_dependencies_and_unknown_dependencies_fail_closed(self):
        config = '''<config>
  <moduleDependencies><gameDependency version="1.6.1170.0" /><fommDependency version="0.13.0" /><foseDependency version="0.2.2.6" /></moduleDependencies>
  <installSteps><installStep name="Main"><optionalFileGroups><group name="Choice" type="SelectExactlyOne"><plugins>
    <plugin name="Only"><typeDescriptor><type name="Recommended" /></typeDescriptor><files><file source="file.txt" /></files></plugin>
  </plugins></group></optionalFileGroups></installStep></installSteps>
</config>'''
        archive = self.make_archive(config, {"file.txt": b"file"})

        valid = plan_archive(archive, game_version="1.6.1170.0", script_extender_version="0.2.2.6")
        self.assertEqual(valid["errors"], [])
        invalid = plan_archive(archive, game_version="1.5.97.0", script_extender_version="0.2.2.6")
        self.assertIn("Module dependencies are not satisfied", invalid["errors"])

        unknown = self.make_archive('''<config><installSteps><installStep name="Main"><visible><unknownDependency /></visible></installStep></installSteps></config>''')
        with self.assertRaisesRegex(Mo2Error, "Unsupported FOMOD dependency element"):
            plan_archive(unknown)

    def test_contextual_inspector_uses_detected_game_version(self):
        game = self.root / "Game"
        game.mkdir()
        (game / "SkyrimSE.exe").write_bytes(b"exe")
        ini = self.root / "ModOrganizer.ini"
        ini.write_text(ini.read_text(encoding="utf-8").replace(
            "gameName=Skyrim Special Edition\n",
            f"gameName=Skyrim Special Edition\ngamePath={game}\n",
        ), encoding="utf-8")
        instance = Instance.open(self.root)
        archive = self.make_archive('''<config><installSteps><installStep name="Main"><optionalFileGroups><group name="Runtime" type="SelectExactlyOne"><plugins>
          <plugin name="AE"><typeDescriptor><dependencyType><defaultType name="NotUsable" /><patterns><pattern><dependencies><gameDependency version="1.6.1170.0" /></dependencies><type name="Recommended" /></pattern></patterns></dependencyType></typeDescriptor><files><file source="ae.txt" /></files></plugin>
        </plugins></group></optionalFileGroups></installStep></installSteps></config>''', {"ae.txt": b"ae"})

        with patch("mo2cli.workspace._windows_file_version", return_value="1.6.1170.0"):
            inspected = inspect_archive(archive, instance=instance, profile="Default")

        self.assertTrue(inspected["contextual"])
        self.assertEqual(inspected["context"]["game_version"], "1.6.1170.0")
        self.assertEqual(inspected["selected"], ["AE"])
        self.assertEqual(inspected["steps"][0]["groups"][0]["options"][0]["type"], "Recommended")


if __name__ == "__main__":
    unittest.main()
