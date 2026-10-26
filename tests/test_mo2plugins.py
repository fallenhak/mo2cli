import io
import json
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

from mo2cli import mo2plugins
from mo2cli.cli import main
from mo2cli.downloads import sync_downloads
from mo2cli.journal import history, undo_last
from mo2cli.metadata import IniDocument
from mo2cli.workspace import Instance, Mo2Error


def make_zip(path: Path, members: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as output:
        for name, data in members.items():
            output.writestr(name, data)
    return path


class Mo2PluginTests(unittest.TestCase):
    def setUp(self):
        from test_workspace import WorkspaceTests

        self.fixture = WorkspaceTests()
        self.fixture.setUp()
        self.root = self.fixture.make_instance()
        (self.root / "plugins").mkdir()
        (self.root / "downloads").mkdir(exist_ok=True)
        self.instance = Instance.open(self.root)
        self.incoming = self.root / "incoming"
        self.incoming.mkdir()

    def tearDown(self):
        self.fixture.tearDown()

    def meta(self, name: str) -> dict:
        return IniDocument.read(self.root / "downloads" / f"{name}.meta").as_dict("General")

    def fomod_plus(self, name="FOMOD Plus-1.9.2.zip", extra=None):
        members = {"plugins/FOMOD Plus/fomod_plus.dll": b"dll", "plugins/FOMOD Plus/res/icon.png": b"png", "plugins/data/FomodPlus.json": b"{}", "README.txt": b"hi"}
        members.update(extra or {})
        return make_zip(self.incoming / name, members)

    def test_plugins_dir_layout_installs_contents_and_ignores_the_rest(self):
        result = mo2plugins.install(self.instance, self.fomod_plus())
        self.assertEqual(result["layout"], "plugins-dir")
        self.assertEqual(result["ignored"], ["README.txt"])
        self.assertTrue((self.root / "plugins" / "FOMOD Plus" / "fomod_plus.dll").is_file())
        self.assertTrue((self.root / "plugins" / "data" / "FomodPlus.json").is_file())
        self.assertFalse((self.root / "plugins" / "README.txt").exists())
        self.assertEqual(result["name"], "FOMOD Plus-1.9.2")

    def test_archive_and_meta_are_kept_in_downloads_as_installed(self):
        result = mo2plugins.install(self.instance, self.fomod_plus(), direct_url="https://github.com/o/r/releases/download/v1.9.2/FOMOD.Plus.zip")
        self.assertEqual(result["wabbajack_source"], "directURL")
        self.assertTrue((self.root / "downloads" / "FOMOD Plus-1.9.2.zip").is_file())
        values = self.meta("FOMOD Plus-1.9.2.zip")
        self.assertIs(values["installed"], True)
        self.assertIs(values["uninstalled"], False)
        self.assertEqual(values["directURL"], "https://github.com/o/r/releases/download/v1.9.2/FOMOD.Plus.zip")
        self.assertEqual(values["modName"], "FOMOD Plus-1.9.2")

    def test_install_without_source_keeps_existing_meta_keys_and_notes_missing_source(self):
        archive = self.fomod_plus()
        (self.root / "downloads" / archive.name).write_bytes(archive.read_bytes())
        (self.root / "downloads" / f"{archive.name}.meta").write_text("[General]\ngameName=SkyrimSE\nmodID=2088\nfileID=7\nnewestVersion=9\n")
        result = mo2plugins.install(self.instance, archive)
        self.assertEqual(result["wabbajack_source"], "nexus")
        self.assertNotIn("note", result)
        values = self.meta(archive.name)
        self.assertEqual(values["newestVersion"], 9)
        self.assertIs(values["installed"], True)

    def test_plugin_folder_layout(self):
        archive = make_zip(self.incoming / "Root Builder 5.1.1.zip", {"rootbuilder/__init__.py": b"", "rootbuilder/rb/core.py": b"x"})
        result = mo2plugins.install(self.instance, archive)
        self.assertEqual(result["layout"], "plugin-folder")
        self.assertEqual(result["name"], "rootbuilder")
        self.assertTrue((self.root / "plugins" / "rootbuilder" / "rb" / "core.py").is_file())

    def test_wrapper_folder_around_plugins_dir(self):
        archive = make_zip(self.incoming / "wrapped.zip", {"Release/plugins/rootbuilder/__init__.py": b"", "Release/notes.txt": b"n"})
        result = mo2plugins.install(self.instance, archive)
        self.assertEqual(result["layout"], "plugins-dir")
        self.assertTrue((self.root / "plugins" / "rootbuilder" / "__init__.py").is_file())

    def test_loose_python_and_dll_files(self):
        archive = make_zip(self.incoming / "Loose Plugin.zip", {"loose_plugin.py": b"x", "helper.dll": b"d", "readme.md": b"r", "lib/util.py": b"u"})
        result = mo2plugins.install(self.instance, archive)
        self.assertEqual(result["layout"], "loose")
        self.assertEqual(result["name"], "Loose Plugin")
        self.assertTrue((self.root / "plugins" / "loose_plugin.py").is_file())
        self.assertTrue((self.root / "plugins" / "lib" / "util.py").is_file())
        self.assertFalse((self.root / "plugins" / "readme.md").exists())

    def test_ambiguous_layout_needs_source(self):
        archive = make_zip(self.incoming / "multi.zip", {"a/x.txt": b"1", "b/rootbuilder/__init__.py": b"2"})
        with self.assertRaises(Mo2Error):
            mo2plugins.install(self.instance, archive)
        result = mo2plugins.install(self.instance, archive, source="b")
        self.assertEqual(result["layout"], "plugin-folder")
        with self.assertRaises(Mo2Error):
            mo2plugins.install(self.instance, archive, source="missing")

    def test_conflicts_need_replace_and_are_moved_to_trash(self):
        existing = self.root / "plugins" / "FOMOD Plus" / "fomod_plus.dll"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"old")
        with self.assertRaises(Mo2Error):
            mo2plugins.install(self.instance, self.fomod_plus())
        self.assertEqual(existing.read_bytes(), b"old")
        self.assertFalse((self.root / "plugins" / "data").exists())
        result = mo2plugins.install(self.instance, self.fomod_plus(), replace=True)
        self.assertEqual(result["overwrites"], ["FOMOD Plus/fomod_plus.dll"])
        self.assertEqual(existing.read_bytes(), b"dll")
        backups = list((self.root / ".mo2cli-trash").rglob("fomod_plus.dll"))
        self.assertEqual([item.read_bytes() for item in backups], [b"old"])

    def test_manifest_and_journal_are_written(self):
        mo2plugins.install(self.instance, self.fomod_plus(), name="FOMOD Plus")
        manifest = json.loads((self.root / ".mo2cli" / "mo2plugins.json").read_text())
        record = manifest["plugins"]["FOMOD Plus"]
        self.assertEqual(record["archive"], "FOMOD Plus-1.9.2.zip")
        self.assertEqual(record["files"], ["data/FomodPlus.json", "FOMOD Plus/fomod_plus.dll", "FOMOD Plus/res/icon.png"])
        self.assertEqual(len(record["archive_sha256"]), 64)
        self.assertEqual(history(self.instance, 1)[0]["operation"], "mo2plugin_install")

    def test_duplicate_install_needs_replace_and_replace_drops_stale_files(self):
        mo2plugins.install(self.instance, self.fomod_plus(), name="FP")
        with self.assertRaises(Mo2Error):
            mo2plugins.install(self.instance, self.fomod_plus(), name="FP")
        newer = self.fomod_plus("FOMOD Plus-2.0.zip", {"plugins/FOMOD Plus/new.dll": b"new"})
        # the newer archive no longer ships icon.png
        with zipfile.ZipFile(newer) as source:
            members = {name: source.read(name) for name in source.namelist() if not name.endswith("icon.png")}
        newer = make_zip(self.incoming / "FOMOD Plus-2.0.zip", members)
        result = mo2plugins.install(self.instance, newer, name="FP", replace=True)
        self.assertTrue(result["replaces_existing"])
        self.assertFalse((self.root / "plugins" / "FOMOD Plus" / "res").exists())
        self.assertTrue((self.root / "plugins" / "FOMOD Plus" / "new.dll").is_file())
        listing = mo2plugins.list_installed(self.instance)
        self.assertEqual([item["name"] for item in listing], ["FP"])
        self.assertEqual(listing[0]["archive"], "FOMOD Plus-2.0.zip")

    def test_dry_run_changes_nothing(self):
        result = mo2plugins.install(self.instance, self.fomod_plus(), dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertFalse((self.root / "plugins" / "FOMOD Plus").exists())
        self.assertFalse((self.root / ".mo2cli").exists())
        self.assertFalse((self.root / "downloads" / "FOMOD Plus-1.9.2.zip").exists())

    def test_zip_slip_is_rejected(self):
        archive = make_zip(self.incoming / "evil.zip", {"plugins/ok.py": b"1", "plugins/../../escaped.py": b"2"})
        with self.assertRaises(Mo2Error):
            mo2plugins.install(self.instance, archive)
        self.assertFalse((self.root / "escaped.py").exists())
        self.assertFalse((self.root / "plugins" / "ok.py").exists())

    def test_requires_portable_plugins_folder(self):
        (self.root / "plugins").rmdir()
        with self.assertRaises(Mo2Error):
            mo2plugins.install(self.instance, self.fomod_plus())

    def test_list_reports_missing_files_and_remove_moves_to_trash(self):
        mo2plugins.install(self.instance, self.fomod_plus(), name="FP")
        (self.root / "plugins" / "data" / "FomodPlus.json").unlink()
        listing = mo2plugins.list_installed(self.instance)
        self.assertEqual((listing[0]["files"], listing[0]["missing_files"]), (3, 1))
        result = mo2plugins.remove(self.instance, "fp")
        self.assertEqual(result["files"], 2)
        self.assertFalse((self.root / "plugins" / "FOMOD Plus").exists())
        self.assertFalse((self.root / "plugins" / "data").exists())  # emptied folders are cleaned up
        self.assertTrue((self.root / "plugins").is_dir())
        self.assertTrue(any((self.root / ".mo2cli-trash").rglob("fomod_plus.dll")))
        self.assertEqual(mo2plugins.list_installed(self.instance), [])
        self.assertIs(self.meta("FOMOD Plus-1.9.2.zip")["installed"], False)
        self.assertTrue((self.root / "downloads" / "FOMOD Plus-1.9.2.zip").is_file())  # archive stays for Wabbajack
        with self.assertRaises(Mo2Error):
            mo2plugins.remove(self.instance, "FP")

    def test_remove_dry_run(self):
        mo2plugins.install(self.instance, self.fomod_plus(), name="FP")
        result = mo2plugins.remove(self.instance, "FP", dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertTrue((self.root / "plugins" / "FOMOD Plus" / "fomod_plus.dll").is_file())

    def test_undo_install_removes_files_and_restores_manifest_and_meta(self):
        mo2plugins.install(self.instance, self.fomod_plus(), name="FP")
        undone = undo_last(self.instance)
        self.assertEqual(undone["undone"], "mo2plugin_install")
        self.assertFalse((self.root / "plugins" / "FOMOD Plus").exists())
        self.assertFalse((self.root / "plugins" / "data").exists())
        self.assertFalse((self.root / ".mo2cli" / "mo2plugins.json").exists())
        self.assertFalse((self.root / "downloads" / "FOMOD Plus-1.9.2.zip.meta").exists())

    def test_undo_install_restores_replaced_foreign_files(self):
        existing = self.root / "plugins" / "FOMOD Plus" / "fomod_plus.dll"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"old")
        mo2plugins.install(self.instance, self.fomod_plus(), name="FP", replace=True)
        undo_last(self.instance)
        self.assertEqual(existing.read_bytes(), b"old")
        self.assertFalse((self.root / "plugins" / "FOMOD Plus" / "res").exists())

    def test_undo_remove_restores_files_manifest_and_meta(self):
        mo2plugins.install(self.instance, self.fomod_plus(), name="FP")
        mo2plugins.remove(self.instance, "FP")
        undone = undo_last(self.instance)
        self.assertEqual(undone["undone"], "mo2plugin_remove")
        self.assertEqual((self.root / "plugins" / "FOMOD Plus" / "res" / "icon.png").read_bytes(), b"png")
        self.assertEqual([item["name"] for item in mo2plugins.list_installed(self.instance)], ["FP"])
        self.assertIs(self.meta("FOMOD Plus-1.9.2.zip")["installed"], True)

    def test_undo_refuses_when_meta_changed_afterwards(self):
        mo2plugins.install(self.instance, self.fomod_plus(), name="FP")
        with (self.root / "downloads" / "FOMOD Plus-1.9.2.zip.meta").open("a") as output:
            output.write("changed=1\n")
        with self.assertRaises(Mo2Error):
            undo_last(self.instance)
        self.assertTrue((self.root / "plugins" / "FOMOD Plus" / "fomod_plus.dll").is_file())

    def test_downloads_sync_recognises_fomod_plus_and_managed_plugin_archives(self):
        make_zip(self.root / "downloads" / "FOMOD Plus 1.9.2-2088-1-9-2.zip", {"a": b"1"})
        make_zip(self.root / "downloads" / "Some Other Plugin.zip", {"b": b"2"})
        make_zip(self.root / "downloads" / "Unrelated.zip", {"c": b"3"})
        (self.root / "plugins" / "FOMOD Plus").mkdir()
        (self.root / "plugins" / "FOMOD Plus" / "fomod_plus.dll").write_bytes(b"d")
        mo2plugins.install(self.instance, make_zip(self.root / "downloads" / "Some Other Plugin.zip", {"other/__init__.py": b""}), name="other")
        for name in ("FOMOD Plus 1.9.2-2088-1-9-2.zip", "Some Other Plugin.zip"):
            (self.root / "downloads" / f"{name}.meta").write_text("[General]\ninstalled=false\n")
        result = sync_downloads(self.instance)
        reasons = {item["archive"]: item["reason"] for item in result["updated"]}
        self.assertIn("FOMOD Plus", reasons["FOMOD Plus 1.9.2-2088-1-9-2.zip"])
        self.assertEqual(result["unmatched"], ["Unrelated.zip"])

    def test_cli_install_list_remove_and_undo(self):
        archive = self.fomod_plus()
        base = ["--instance", str(self.root)]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(base + ["mo2plugin", "install", str(archive), "--name", "FP", "--dry-run"]), 0)
            self.assertFalse((self.root / "plugins" / "FOMOD Plus").exists())
            self.assertEqual(main(base + ["mo2plugin", "install", str(archive), "--name", "FP"]), 0)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(main(["--json"] + base + ["mo2plugin", "list"]), 0)
        self.assertEqual(json.loads(buffer.getvalue())[0]["name"], "FP")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(base + ["mo2plugin", "remove", "FP"]), 0)
            self.assertEqual(main(base + ["undo"]), 0)
            self.assertEqual(main(base + ["mo2plugin", "remove", "Nope"]), 2)
        self.assertTrue((self.root / "plugins" / "FOMOD Plus" / "fomod_plus.dll").is_file())


if __name__ == "__main__":
    unittest.main()
