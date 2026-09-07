import json
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from mo2cli.archives import list_archive, sha256
from mo2cli.installer import install_archive, remove_mod, rename_mod
from mo2cli.journal import undo_last
from mo2cli.manifest import apply as apply_manifest
from mo2cli.nexus import NexusReference, download_reference, parse_reference
from mo2cli.cli import main
from mo2cli.separators import create as create_separator, group as group_separator, remove as remove_separator
from mo2cli.inis import get_value, set_value
from mo2cli.instance import initialize
from mo2cli.fomod import inspect_archive, plan_archive
from mo2cli.usvfs import status as usvfs_status
from mo2cli.tool_workflows import ensure_output_mod, run_bodyslide
from mo2cli.profiles import export_profile, import_profile
from mo2cli.plugins import analyze as analyze_plugins, catalog as plugin_catalog
from mo2cli.workspace import Instance, Mo2Error
from mo2cli.formats import ModList
from mo2cli.metadata import ModMetadata, sync_metadata


class FeatureTests(unittest.TestCase):
    def setUp(self):
        from test_workspace import WorkspaceTests

        self.fixture = WorkspaceTests()
        self.fixture.setUp()
        self.root = self.fixture.make_instance()
        self.instance = Instance.open(self.root)

    def tearDown(self):
        self.fixture.tearDown()

    def test_simple_archive_install_writes_metadata_and_modlist(self):
        archive = self.root / "downloads" / "Example.zip"
        archive.parent.mkdir()
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("Data/meshes/example.nif", b"nif")
            output.writestr("Data/textures/example.dds", b"dds")
        (Path(f"{archive}.meta")).write_text("[General]\ninstalled=false\nuninstalled=false\n", encoding="utf-8")
        result = install_archive(self.instance, archive, "Default", name="Example Mod")
        self.assertEqual(result["name"], "Example Mod")
        self.assertTrue((self.root / "mods" / "Example Mod" / "meshes" / "example.nif").exists())
        self.assertEqual(self.instance.mod_metadata("Example Mod").get("installationFile"), archive.name)
        self.assertIn("installed=true", Path(f"{archive}.meta").read_text(encoding="utf-8"))
        self.assertIsNotNone(self.instance.profile_files("Default")[1].find("Example Mod"))
        self.assertEqual(len(list_archive(archive)), 2)
        self.assertEqual(len(sha256(archive)), 64)

    def test_simple_archive_keeps_nested_game_data_directories(self):
        archive = self.root / "downloads" / "Clouds.zip"
        archive.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("Data/textures/sky/cloud.dds", b"dds")
        install_archive(self.instance, archive, "Default", name="Clouds Mod")
        self.assertTrue((self.root / "mods" / "Clouds Mod" / "textures" / "sky" / "cloud.dds").exists())
        self.assertFalse((self.root / "mods" / "Clouds Mod" / "cloud.dds").exists())
        self.assertEqual(self.instance.mod_metadata("Clouds Mod").get("gameName"), "SkyrimSE")

    def test_metadata_sync_imports_nexus_download_sidecar(self):
        archive = self.root / "downloads" / "Metadata Sync-123-1-0.zip"
        archive.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("Data/metadata.txt", b"metadata")
        (Path(f"{archive}.meta")).write_text(
            "[General]\nmodID=123\nfileID=456\nversion=1.0\nrepository=Nexus\n",
            encoding="utf-8",
        )
        install_archive(self.instance, archive, "Default", name="Metadata Mod")
        result = sync_metadata(self.instance, "Default")
        self.assertEqual(result["updated"][0]["fileID"], 456)
        metadata = ModMetadata.read(self.root / "mods" / "Metadata Mod")
        self.assertEqual(metadata.get("modID"), 123)
        self.assertEqual(metadata.get("fileID"), 456)
        self.assertEqual(metadata.get("url"), "https://www.nexusmods.com/skyrimspecialedition/mods/123?tab=files&file_id=456")
        sidecar = Path(f"{archive}.meta").read_text(encoding="utf-8")
        self.assertIn("modName=Metadata Mod", sidecar)
        self.assertIn("installed=true", sidecar)
        self.assertIn("uninstalled=false", sidecar)

    def test_undo_install_and_remove(self):
        archive = self.root / "downloads" / "Undo.zip"
        archive.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("Data/undo.txt", b"undo")
        installed = install_archive(self.instance, archive, "Default", name="Undo Mod")
        self.assertTrue(Path(installed["path"]).is_dir())
        self.assertEqual(undo_last(self.instance)["undone"], "install")
        self.assertFalse((self.root / "mods" / "Undo Mod").exists())
        installed = install_archive(self.instance, archive, "Default", name="Undo Mod")
        remove_mod(self.instance, "Undo Mod", yes=True)
        self.assertEqual(undo_last(self.instance)["undone"], "remove")
        self.assertTrue(Path(installed["path"]).is_dir())

    def test_separator_create_group_remove_and_undo(self):
        created = create_separator(self.instance, "Default", "Core")
        self.assertEqual(created["internal_name"], "Core_separator")
        self.assertTrue((self.root / "mods" / "Core_separator").is_dir())
        group_separator(self.instance, "Default", "Core", ["Patch Mod", "Base Mod"])
        names = [entry.name for entry in self.instance.profile_files("Default")[1].entries]
        self.assertEqual(names[-3:], ["Base Mod", "Patch Mod", "Core_separator"])
        self.assertEqual(undo_last(self.instance)["undone"], "separator_group")
        remove_separator(self.instance, "Default", "Core", yes=True)
        self.assertFalse((self.root / "mods" / "Core_separator").exists())
        self.assertEqual(undo_last(self.instance)["undone"], "separator_remove")
        self.assertTrue((self.root / "mods" / "Core_separator").is_dir())

    def test_manifest_applies_local_archive_and_separator_group(self):
        archive = self.root / "downloads" / "Manifest.zip"
        archive.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("Data/manifest.txt", b"manifest")
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({
            "profile": "Default",
            "separators": [{"name": "Manifest Group", "mods": ["Manifest Mod"]}],
            "mods": [{"name": "Manifest Mod", "path": "downloads/Manifest.zip", "separator": "Manifest Group"}],
        }), encoding="utf-8")
        result = apply_manifest(self.instance, manifest)
        self.assertEqual(result["profile"], "Default")
        self.assertTrue((self.root / "mods" / "Manifest Mod" / "manifest.txt").exists())
        names = [entry.name for entry in self.instance.profile_files("Default")[1].entries]
        index = names.index("Manifest Group_separator")
        self.assertEqual(names[index - 1:index + 1], ["Manifest Mod", "Manifest Group_separator"])

    def test_manifest_auto_creates_referenced_separator_and_writes_source_alias(self):
        archive = self.root / "downloads" / "source-alias.zip"
        archive.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("Data/source-alias.txt", b"source")
        manifest = self.root / "source-alias.json"
        manifest.write_text(json.dumps({
            "mods": [{"name": "Source Alias", "source": "downloads/source-alias.zip", "separator": "Auto Group"}]
        }), encoding="utf-8")
        apply_manifest(self.instance, manifest)
        self.assertTrue((self.root / "mods" / "Source Alias" / "source-alias.txt").exists())
        names = [entry.name for entry in ModList.read(self.root / "profiles" / "Default" / "modlist.txt").entries]
        index = names.index("Auto Group_separator")
        self.assertEqual(names[index - 1:index + 1], ["Source Alias", "Auto Group_separator"])
        self.assertEqual(ModMetadata.read(self.root / "mods" / "Source Alias").get("gameName"), "SkyrimSE")

    def test_nxm_reference_parsing(self):
        reference = parse_reference("nxm://skyrimspecialedition/mods/123/files/456?key=k&expires=99", self.instance)
        self.assertEqual((reference.game, reference.mod_id, reference.file_id, reference.key), ("skyrimspecialedition", 123, 456, "k"))
        friendly = parse_reference("https://www.nexusmods.com/skyrimspecialedition/mods/123", self.instance, "Skyrim Special Edition")
        self.assertEqual(friendly.game, "skyrimspecialedition")
        with self.assertRaises(Mo2Error):
            parse_reference("https://www.nexusmods.com/skyrimspecialedition/mods/123/files/not-a-number", self.instance)

    def test_nxm_download_link_uses_api_key_and_optional_nxm_token(self):
        responses = [b'[{"URI":"https://files.example.test/mod.zip"}]', b"archive-bytes"]

        class Response:
            def __init__(self, content):
                self.content = content

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size=-1):
                if size < 0:
                    return self.content
                result, self.content = self.content[:size], self.content[size:]
                return result

        def fake_urlopen(request, timeout=60):
            return Response(responses.pop(0))

        with patch("mo2cli.nexus.urllib.request.urlopen", side_effect=fake_urlopen), patch("mo2cli.downloads.urllib.request.urlopen", side_effect=fake_urlopen):
            result = download_reference(self.instance, NexusReference("skyrimspecialedition", 123, 456, "key", "999"), api_key="test-api", output="nxm-test.zip")
        self.assertEqual(result["source"], "nexus")
        self.assertEqual((self.instance.downloads_dir / "nxm-test.zip").read_bytes(), b"archive-bytes")

    def test_separator_cli_commands(self):
        self.assertEqual(main(["--instance", str(self.root), "--profile", "Default", "mods", "separator", "create", "CLI Group"]), 0)
        self.assertEqual(main(["--instance", str(self.root), "--profile", "Default", "mods", "separator", "group", "CLI Group", "Patch Mod"]), 0)
        self.assertTrue((self.root / "mods" / "CLI Group_separator").is_dir())

    def test_mod_install_accepts_remote_source_via_download_layer(self):
        archive = self.root / "downloads" / "Remote.zip"
        archive.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("Data/remote.txt", b"remote")
        with patch("mo2cli.cli.fetch_download", return_value={"path": str(archive), "url": "https://example.test/remote.zip"}):
            result = main(["--instance", str(self.root), "--profile", "Default", "mods", "install", "https://example.test/remote.zip", "--name", "Remote Mod", "--separator", "Remote Group"])
        self.assertEqual(result, 0)
        self.assertTrue((self.root / "mods" / "Remote Mod" / "remote.txt").exists())
        self.assertTrue((self.root / "mods" / "Remote Group_separator").is_dir())

    def test_fomod_requires_explicit_opt_in(self):
        archive = self.root / "downloads" / "Fomod.zip"
        archive.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("fomod/ModuleConfig.xml", b"<config/>")
            output.writestr("Data/file.txt", b"file")
        with self.assertRaises(Mo2Error):
            install_archive(self.instance, archive, "Default")
        archive_info = inspect_archive(archive)
        self.assertEqual(archive_info["steps"], [])

    def test_fomod_plan_and_install_apply_selection_and_priority(self):
        archive = self.root / "downloads" / "Choice.zip"
        archive.parent.mkdir(exist_ok=True)
        config = b'''<?xml version="1.0" encoding="UTF-8"?>
<config>
  <moduleName>Fancy Mod</moduleName>
  <requiredInstallFiles><files><file source="Data/base.txt" destination="base.txt" /></files></requiredInstallFiles>
  <installSteps order="Explicit">
    <installStep name="Main">
      <optionalFileGroups>
        <group name="Variant" type="SelectExactlyOne">
          <plugins>
            <plugin name="Recommended A"><description>A</description><typeDescriptor><type name="Recommended" /></typeDescriptor><conditionFlags><flag name="variant">A</flag></conditionFlags><files><file source="Data/a.txt" destination="variant.txt" priority="10" /></files></plugin>
            <plugin name="Optional B"><typeDescriptor><type name="Optional" /></typeDescriptor><files><file source="Data/b.txt" destination="variant.txt" priority="10" /></files></plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
  </installSteps>
  <conditionalFileInstalls><patterns><pattern><dependencies><flagDependency flag="variant" value="A" /></dependencies><files><file source="Data/conditional.txt" destination="conditional.txt" /></files></pattern></patterns></conditionalFileInstalls>
</config>'''
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("fomod/ModuleConfig.xml", config)
            output.writestr("Data/base.txt", b"base")
            output.writestr("Data/a.txt", b"a")
            output.writestr("Data/b.txt", b"b")
            output.writestr("Data/conditional.txt", b"conditional")
        plan = plan_archive(archive)
        self.assertEqual(plan["module_name"], "Fancy Mod")
        self.assertEqual(plan["selected"], ["Recommended A"])
        installed = install_archive(self.instance, archive, "Default", allow_fomod=True)
        mod_root = Path(installed["path"])
        self.assertEqual(mod_root.name, "Fancy Mod")
        self.assertEqual((mod_root / "variant.txt").read_bytes(), b"a")
        self.assertTrue((mod_root / "conditional.txt").exists())
        decisions = self.root / "profiles" / "Default" / ".mo2cli" / "fomod-decisions.json"
        self.assertTrue(decisions.exists())
        saved = json.loads(decisions.read_text(encoding="utf-8"))["decisions"][-1]
        self.assertEqual(saved["selections"]["Main/Variant"], ["Recommended A"])
        self.assertEqual(set(saved["context"]["mods"]), {"Base Mod", "Patch Mod"})

        reused = install_archive(self.instance, archive, "Default", replace=True, allow_fomod=True, fomod_reuse="always")
        self.assertEqual(reused["selected"], ["Recommended A"])
        self.assertEqual(reused["decision"]["source"], "saved")

        selected_b = plan_archive(archive, {"Main/Variant": ["Optional B"]})
        self.assertEqual(selected_b["selected"], ["Optional B"])
        self.assertFalse(any(item["source"] == "Data/conditional.txt" for item in selected_b["operations"]))

        deselected = plan_archive(archive, {"Main/Variant": []})
        self.assertEqual(deselected["selected"], [])
        self.assertTrue(any("at least one selection" in error for error in deselected["errors"]))

    def test_archive_path_traversal_is_rejected(self):
        archive = self.root / "downloads" / "unsafe.zip"
        archive.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("../outside.txt", b"no")
        with self.assertRaises(Mo2Error):
            install_archive(self.instance, archive, "Default")

    def test_profile_export_import_roundtrip(self):
        archive = self.root / "Default-profile.zip"
        exported = export_profile(self.instance, "Default", archive)
        self.assertEqual(exported["profile"], "Default")
        imported = import_profile(self.instance, archive, "Imported")
        self.assertEqual(imported["profile"], "Imported")
        self.assertTrue((self.root / "profiles" / "Imported" / "modlist.txt").exists())

    def test_rename_and_recoverable_remove(self):
        renamed = rename_mod(self.instance, "Patch Mod", "Renamed Patch")
        self.assertTrue(Path(renamed["path"]).is_dir())
        self.assertIsNotNone(self.instance.profile_files("Default")[1].find("Renamed Patch"))
        removed = remove_mod(self.instance, "Renamed Patch", yes=True)
        self.assertFalse((self.root / "mods" / "Renamed Patch").exists())
        self.assertTrue(Path(removed["trash"]).is_dir())

    def test_plugin_catalog_and_missing_master_check(self):
        payload = b"MAST" + struct.pack("<H", len(b"Missing.esm\0")) + b"Missing.esm\0"
        header = (
            b"TES4"
            + struct.pack("<I", len(payload))
            + struct.pack("<I", 0)
            + b"\0" * 8
            + struct.pack("<H", 44)
            + b"\0" * 2
        )
        plugin = self.root / "mods" / "Patch Mod" / "Test.esp"
        plugin.write_bytes(header + payload)
        catalog = plugin_catalog(self.instance, "Default")
        test_plugin = next(item for item in catalog if item["name"] == "Test.esp")
        self.assertEqual(test_plugin["masters"], ["Missing.esm"])
        self.assertEqual(test_plugin["form_version"], 44)
        issues = analyze_plugins(self.instance, "Default")
        self.assertTrue(any(issue["code"] == "missing-master" for issue in issues))

    def test_profile_ini_roundtrip(self):
        ini = self.root / "profiles" / "Default" / "Skyrim.ini"
        ini.write_text("[Display]\nbFullScreen=1\n", encoding="utf-8")
        self.assertEqual(get_value(self.instance, "Default", "Skyrim.ini", "Display", "bFullScreen"), 1)
        set_value(self.instance, "Default", "Skyrim.ini", "Display", "bFullScreen", "0")
        self.assertEqual(get_value(self.instance, "Default", "Skyrim.ini", "Display", "bFullScreen"), 0)

    def test_instance_initialize_creates_mo2_layout(self):
        root = Path(self.fixture.temp.name) / "new-instance"
        result = initialize(root, "Skyrim Special Edition", self.fixture.temp.name)
        self.assertEqual(result["profile"], "Default")
        created = Instance.open(root)
        self.assertEqual(created.selected_profile, "Default")
        self.assertTrue((root / "profiles" / "Default" / "modlist.txt").exists())

    def test_usvfs_status_is_safe_when_runtime_is_absent(self):
        report = usvfs_status(self.instance)
        self.assertFalse(report["available"])

    def test_tool_output_mod_is_registered_and_grouped(self):
        output = ensure_output_mod(self.instance, "Default", "BodySlide Output", "Tool Outputs")
        self.assertEqual(output, self.root / "mods" / "BodySlide Output")
        self.assertTrue((output / "meta.ini").is_file())
        self.assertIsNotNone(self.instance.profile_files("Default")[1].find("BodySlide Output"))
        self.assertIsNotNone(self.instance.profile_files("Default")[1].find("Tool Outputs_separator"))

    def test_bodyslide_dry_run_builds_official_command_line(self):
        with patch(
            "mo2cli.tool_workflows.find_executable",
            return_value={"binary": str(self.root / "BodySlide.exe")},
        ):
            result = run_bodyslide(
                self.instance,
                "Default",
                "CBBE Curvy",
                ["CBBE", "CBBE Vanilla Outfits"],
                dry_run=True,
            )
        self.assertEqual(result["arguments"][0:2], ["--groupbuild", "CBBE,CBBE Vanilla Outfits"])
        self.assertIn("--targetdir", result["arguments"])
        self.assertIn("--preset", result["arguments"])
        self.assertFalse((self.root / "mods" / "BodySlide Output").exists())
