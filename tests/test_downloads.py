import unittest
import zipfile
from pathlib import Path

from mo2cli.downloads import mark_installed, sync_downloads
from mo2cli.installer import install_archive
from mo2cli.metadata import IniDocument
from mo2cli.workspace import Instance


class DownloadTests(unittest.TestCase):
    def setUp(self):
        from test_workspace import WorkspaceTests

        self.fixture = WorkspaceTests()
        self.fixture.setUp()
        self.root = self.fixture.make_instance()
        self.instance = Instance.open(self.root)
        self.instance.downloads_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.fixture.tearDown()

    def test_mark_installed_and_uninstalled(self):
        archive = self.instance.downloads_dir / "TestMod-1.0.zip"
        archive.write_bytes(b"dummy")
        meta = self.instance.downloads_dir / "TestMod-1.0.zip.meta"
        meta.write_text("[General]\ninstalled=false\nuninstalled=false\n", encoding="utf-8")

        result = mark_installed(self.instance, ["TestMod-1.0.zip"], installed=True)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["installed"])

        doc = IniDocument.read(meta)
        self.assertTrue(doc.get("installed", section="General"))
        self.assertFalse(doc.get("uninstalled", section="General"))

        # Mark as uninstalled
        mark_installed(self.instance, ["TestMod-1.0.zip"], installed=False)
        doc = IniDocument.read(meta)
        self.assertFalse(doc.get("installed", section="General"))
        self.assertTrue(doc.get("uninstalled", section="General"))

    def test_sync_downloads_matches_mod_id_and_tools(self):
        # 1. Base mod installed in mods/
        mod_dir = self.instance.mods_dir / "Lux"
        mod_dir.mkdir(parents=True, exist_ok=True)
        (mod_dir / "meta.ini").write_text(
            "[General]\nmodid=43158\ninstallationFile=Lux (main)-43158-7-0.rar\n",
            encoding="utf-8",
        )

        # 2. Main archive (uninstalled state in .meta)
        main_arch = self.instance.downloads_dir / "Lux (main)-43158-7-0.rar"
        main_arch.write_bytes(b"main")
        (self.instance.downloads_dir / f"{main_arch.name}.meta").write_text(
            "[General]\nmodID=43158\ninstalled=false\nuninstalled=false\n",
            encoding="utf-8",
        )

        # 3. Update archive with matching modID but different filename
        update_arch = self.instance.downloads_dir / "Lux (main plugin update)-43158-7-1.rar"
        update_arch.write_bytes(b"update")
        (self.instance.downloads_dir / f"{update_arch.name}.meta").write_text(
            "[General]\nmodID=43158\ninstalled=false\nuninstalled=false\n",
            encoding="utf-8",
        )

        # 4. Root Builder archive with MO2 plugin installed
        rb_plugin = self.instance.base / "plugins" / "rootbuilder"
        rb_plugin.mkdir(parents=True, exist_ok=True)
        rb_arch = self.instance.downloads_dir / "Root Builder-31720-5-1-1.zip"
        rb_arch.write_bytes(b"rb")
        (self.instance.downloads_dir / f"{rb_arch.name}.meta").write_text(
            "[General]\nmodID=31720\ninstalled=false\nuninstalled=false\n",
            encoding="utf-8",
        )

        # 5. Mod Organizer 2 itself
        (self.instance.base / "ModOrganizer.exe").write_bytes(b"mo2")
        mo2_arch = self.instance.downloads_dir / "Mod Organizer 2-6194-2-5-2.exe"
        mo2_arch.write_bytes(b"mo2exe")
        (self.instance.downloads_dir / f"{mo2_arch.name}.meta").write_text(
            "[General]\ninstalled=false\nuninstalled=false\n",
            encoding="utf-8",
        )

        # 6. An unrelated mod that is NOT installed
        other_arch = self.instance.downloads_dir / "Unrelated-99999.zip"
        other_arch.write_bytes(b"other")
        (self.instance.downloads_dir / f"{other_arch.name}.meta").write_text(
            "[General]\nmodID=99999\ninstalled=false\nuninstalled=false\n",
            encoding="utf-8",
        )

        # Before sync, check doctor warning
        issues = self.instance.doctor("Default")
        uninstalled_warnings = [i for i in issues if i.get("code") == "uninstalled-download"]
        self.assertTrue(len(uninstalled_warnings) >= 1)

        # Run sync
        result = sync_downloads(self.instance)
        updated_names = {item["archive"] for item in result["updated"]}

        self.assertIn("Lux (main)-43158-7-0.rar", updated_names)
        self.assertIn("Lux (main plugin update)-43158-7-1.rar", updated_names)
        self.assertIn("Root Builder-31720-5-1-1.zip", updated_names)
        self.assertIn("Mod Organizer 2-6194-2-5-2.exe", updated_names)
        self.assertNotIn("Unrelated-99999.zip", updated_names)
        self.assertIn("Unrelated-99999.zip", result["unmatched"])

        # Verify .meta files
        for name in [
            "Lux (main)-43158-7-0.rar.meta",
            "Lux (main plugin update)-43158-7-1.rar.meta",
            "Root Builder-31720-5-1-1.zip.meta",
            "Mod Organizer 2-6194-2-5-2.exe.meta",
        ]:
            doc = IniDocument.read(self.instance.downloads_dir / name)
            self.assertTrue(doc.get("installed", section="General"))
            self.assertFalse(doc.get("uninstalled", section="General"))

    def test_install_archive_with_merge(self):
        # Create base archive
        base_arch = self.instance.downloads_dir / "MyMod-1.0.zip"
        with zipfile.ZipFile(base_arch, "w") as z:
            z.writestr("Data/file1.txt", b"file1")
        (self.instance.downloads_dir / f"{base_arch.name}.meta").write_text(
            "[General]\ninstalled=false\nuninstalled=false\n", encoding="utf-8"
        )
        install_archive(self.instance, base_arch, "Default", name="MyMod")
        self.assertTrue((self.instance.mods_dir / "MyMod" / "file1.txt").exists())

        # Create update archive
        update_arch = self.instance.downloads_dir / "MyMod-Update.zip"
        with zipfile.ZipFile(update_arch, "w") as z:
            z.writestr("Data/file2.txt", b"file2")
        (self.instance.downloads_dir / f"{update_arch.name}.meta").write_text(
            "[General]\ninstalled=false\nuninstalled=false\n", encoding="utf-8"
        )

        # Install with merge=True
        result = install_archive(self.instance, update_arch, "Default", name="MyMod", merge=True)
        self.assertTrue(result["merged"])
        self.assertTrue((self.instance.mods_dir / "MyMod" / "file1.txt").exists())
        self.assertTrue((self.instance.mods_dir / "MyMod" / "file2.txt").exists())

        # Check that update archive was marked installed
        doc = IniDocument.read(self.instance.downloads_dir / f"{update_arch.name}.meta")
        self.assertTrue(doc.get("installed", section="General"))
        self.assertFalse(doc.get("uninstalled", section="General"))

    def test_fetch_creates_meta_with_url(self):
        import io
        from unittest.mock import patch
        from mo2cli.downloads import fetch

        dummy_bytes = b"sample_archive_content"
        url = "https://github.com/rfortier/JContainers-rwf/releases/download/v4.2.13.2/JContainers64.7z"

        class DummyResponse:
            def __enter__(self):
                return io.BytesIO(dummy_bytes)
            def __exit__(self, exc_type, exc_val, exc_tb):
                pass

        with patch("urllib.request.urlopen", return_value=DummyResponse()):
            result = fetch(self.instance, url)

        target = Path(result["path"])
        self.assertTrue(target.is_file())
        meta_target = target.with_name(target.name + ".meta")
        self.assertTrue(meta_target.is_file())

        doc = IniDocument.read(meta_target)
        self.assertEqual(doc.get("url", section="General"), url)
        self.assertEqual(doc.get("name", section="General"), target.name)
        self.assertFalse(doc.get("installed", section="General"))
        self.assertTrue(doc.get("uninstalled", section="General"))

