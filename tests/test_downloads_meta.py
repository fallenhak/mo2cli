import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mo2cli.cli import main
from mo2cli.downloads import audit_downloads, fetch, set_meta, write_meta
from mo2cli.metadata import IniDocument
from mo2cli.nexus import NexusReference, download_reference
from mo2cli.workspace import Instance, Mo2Error


class FakeResponse:
    def __init__(self, content: bytes):
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


def general(path: Path) -> dict:
    return IniDocument.read(path).as_dict("General")


class DownloadsMetaTests(unittest.TestCase):
    def setUp(self):
        from test_workspace import WorkspaceTests

        self.fixture = WorkspaceTests()
        self.fixture.setUp()
        self.root = self.fixture.make_instance()
        self.instance = Instance.open(self.root)
        self.downloads = self.root / "downloads"
        self.downloads.mkdir(exist_ok=True)

    def tearDown(self):
        self.fixture.tearDown()

    def archive(self, name="Mod.7z") -> Path:
        path = self.downloads / name
        path.write_bytes(b"data")
        return path

    def test_write_meta_direct_url_and_headers_use_wabbajack_syntax(self):
        meta = self.downloads / "A.7z.meta"
        write_meta(meta, direct_url="https://github.com/o/r/releases/download/v1/A.7z", direct_headers=["Referer: https://x.test", "Cookie: a=b"])
        text = meta.read_text()
        self.assertTrue(text.startswith("[General]"))
        self.assertIn("directURL=https://github.com/o/r/releases/download/v1/A.7z", text)
        self.assertIn("directURLHeaders=Referer: https://x.test|Cookie: a=b", text)

    def test_write_meta_preserves_unknown_keys_and_updates_in_place(self):
        meta = self.downloads / "A.7z.meta"
        meta.write_text("[General]\ninstalled=false\nnewestVersion=3\ncustomKey=keep me\n[Other]\nx=1\n")
        write_meta(meta, manual_url="https://www.loverslab.com/files/file/1-x/", prompt="Please download it", installed=True)
        values = general(meta)
        self.assertEqual(values["customKey"], "keep me")
        self.assertEqual(values["newestVersion"], 3)
        self.assertEqual(values["manualURL"], "https://www.loverslab.com/files/file/1-x/")
        self.assertEqual(values["prompt"], "Please download it")
        self.assertIs(values["installed"], True)
        self.assertIs(values["uninstalled"], False)
        self.assertIn("[Other]\nx=1", meta.read_text())
        write_meta(meta, installed=False)
        self.assertEqual(meta.read_text().count("installed="), 2)  # installed + uninstalled, no duplicates

    def test_write_meta_nexus_requires_all_three_and_numeric_ids(self):
        meta = self.downloads / "A.7z.meta"
        write_meta(meta, game_name="SkyrimSE", mod_id=3863, file_id="1000172397")
        self.assertEqual(general(meta)["modID"], 3863)
        self.assertEqual(general(meta)["fileID"], 1000172397)
        self.assertEqual(general(meta)["gameName"], "SkyrimSE")
        with self.assertRaises(Mo2Error):
            write_meta(meta, game_name="SkyrimSE", mod_id=1)
        with self.assertRaises(Mo2Error):
            write_meta(meta, game_name="SkyrimSE", mod_id="abc", file_id=1)
        with self.assertRaises(Mo2Error):
            write_meta(meta, mod_id=1, file_id=2)

    def test_write_meta_rejects_bad_urls_and_orphan_options(self):
        meta = self.downloads / "A.7z.meta"
        with self.assertRaises(Mo2Error):
            write_meta(meta, direct_url="ftp://example.test/a.7z")
        with self.assertRaises(Mo2Error):
            write_meta(meta, direct_url="not a url")
        with self.assertRaises(Mo2Error):
            write_meta(meta, direct_headers=["A: b"])
        with self.assertRaises(Mo2Error):
            write_meta(meta, prompt="hello")
        with self.assertRaises(Mo2Error):
            write_meta(meta, direct_url="https://x.test/a", direct_headers=["bad header"])
        self.assertFalse(meta.exists())

    def test_fetch_writes_direct_url_and_keeps_mo2_url_key(self):
        with patch("mo2cli.downloads.urllib.request.urlopen", return_value=FakeResponse(b"payload")):
            result = fetch(self.instance, "https://example.test/files/My Mod 1.0.zip")
        meta = self.downloads / "My Mod 1.0.zip.meta"
        values = general(meta)
        self.assertEqual(values["url"], "https://example.test/files/My Mod 1.0.zip")
        self.assertEqual(values["directURL"], "https://example.test/files/My%20Mod%201.0.zip")
        self.assertEqual(values["name"], "My Mod 1.0.zip")
        self.assertIs(values["installed"], False)
        self.assertIs(values["uninstalled"], True)
        self.assertIs(values["removed"], False)
        self.assertEqual(result["direct_url"], values["directURL"])

    def test_fetch_records_headers_and_keeps_existing_installed_state(self):
        (self.downloads / "h.zip.meta").write_text("[General]\ninstalled=true\nuninstalled=false\n")
        with patch("mo2cli.downloads.urllib.request.urlopen", return_value=FakeResponse(b"payload")):
            fetch(self.instance, "https://example.test/h.zip", replace=True, headers={"Referer": "https://example.test"})
        values = general(self.downloads / "h.zip.meta")
        self.assertEqual(values["directURLHeaders"], "Referer: https://example.test")
        self.assertIs(values["installed"], True)

    def test_mods_install_url_gets_direct_url_meta(self):
        def fake_urlopen(request, timeout=60):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as output:
                output.writestr("Data/remote.txt", b"remote")
            return FakeResponse(buffer.getvalue())

        with patch("mo2cli.downloads.urllib.request.urlopen", side_effect=fake_urlopen):
            code = main(["--instance", str(self.root), "--profile", "Default", "mods", "install", "https://example.test/dl/Remote.zip", "--name", "Remote Mod"])
        self.assertEqual(code, 0)
        values = general(self.downloads / "Remote.zip.meta")
        self.assertEqual(values["directURL"], "https://example.test/dl/Remote.zip")
        self.assertIs(values["installed"], True)

    def test_manifest_url_entry_gets_direct_url_meta(self):
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"mods": [{"url": "https://example.test/dl/Manifest.zip", "name": "Manifest Mod"}]}))

        def fake_urlopen(request, timeout=60):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as output:
                output.writestr("Data/m.txt", b"m")
            return FakeResponse(buffer.getvalue())

        with patch("mo2cli.downloads.urllib.request.urlopen", side_effect=fake_urlopen):
            code = main(["--instance", str(self.root), "--profile", "Default", "manifest", "apply", str(manifest)])
        self.assertEqual(code, 0)
        self.assertEqual(general(self.downloads / "Manifest.zip.meta")["directURL"], "https://example.test/dl/Manifest.zip")

    def test_nexus_download_writes_nexus_triple_and_never_the_signed_cdn_url(self):
        responses = [b'[{"URI":"https://cdn.test/nexus.zip?sig=abc"}]', b"archive-bytes"]

        def fake_urlopen(request, timeout=60):
            return FakeResponse(responses.pop(0))

        with patch("mo2cli.nexus.urllib.request.urlopen", side_effect=fake_urlopen), patch("mo2cli.downloads.urllib.request.urlopen", side_effect=fake_urlopen):
            download_reference(self.instance, NexusReference("skyrimspecialedition", 123, 456, "key", "999"), api_key="test-api", output="nxm.zip")
        values = general(self.downloads / "nxm.zip.meta")
        self.assertEqual((values["gameName"], values["modID"], values["fileID"]), ("SkyrimSE", 123, 456))
        self.assertNotIn("directURL", values)
        self.assertIs(values["installed"], False)
        self.assertIs(values["uninstalled"], False)
        self.assertEqual(values["repository"], "Nexus")

    def test_set_meta_command_variants(self):
        self.archive("Tool.7z")
        result = set_meta(self.instance, "Tool.7z", direct_url="https://github.com/o/r/releases/download/v1/Tool.7z", headers=["A: b"], installed=True)
        self.assertTrue(result["created"])
        self.assertEqual(result["source"], "directURL")
        values = general(self.downloads / "Tool.7z.meta")
        self.assertEqual(values["name"], "Tool.7z")
        self.assertIs(values["installed"], True)
        self.assertIs(values["removed"], False)
        set_meta(self.instance, "tool.7z", nexus="10:20")
        values = general(self.downloads / "Tool.7z.meta")
        self.assertEqual((values["gameName"], values["modID"], values["fileID"], values["repository"]), ("SkyrimSE", 10, 20, "Nexus"))
        self.assertEqual(values["directURL"], "https://github.com/o/r/releases/download/v1/Tool.7z")  # untouched
        set_meta(self.instance, "Tool.7z", nexus="10:20", game_name="Skyrim")
        self.assertEqual(general(self.downloads / "Tool.7z.meta")["gameName"], "Skyrim")

    def test_set_meta_dry_run_and_validation(self):
        self.archive("Tool.7z")
        result = set_meta(self.instance, "Tool.7z", manual_url="https://example.test/page", prompt="Get it", dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["source"], "manualURL")
        self.assertFalse((self.downloads / "Tool.7z.meta").exists())
        for kwargs in (
            {},
            {"direct_url": "https://a.test/x", "manual_url": "https://b.test/y"},
            {"headers": ["A: b"], "manual_url": "https://b.test/y"},
            {"prompt": "x", "direct_url": "https://a.test/x"},
            {"game_name": "SkyrimSE", "direct_url": "https://a.test/x"},
            {"nexus": "12"},
        ):
            with self.assertRaises(Mo2Error):
                set_meta(self.instance, "Tool.7z", **kwargs)
        with self.assertRaises(Mo2Error):
            set_meta(self.instance, "Missing.7z", direct_url="https://a.test/x")

    def test_set_meta_cli(self):
        self.archive("Cli.7z")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--instance", str(self.root), "--json", "downloads", "meta", "Cli.7z", "--direct-url", "https://a.test/Cli.7z", "--header", "Referer: https://a.test", "--installed"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buffer.getvalue())["source"], "directURL")
        values = general(self.downloads / "Cli.7z.meta")
        self.assertEqual(values["directURLHeaders"], "Referer: https://a.test")
        self.assertIs(values["installed"], True)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--instance", str(self.root), "downloads", "meta", "Cli.7z", "--not-installed"]), 0)
        self.assertIs(general(self.downloads / "Cli.7z.meta")["installed"], False)
        self.assertEqual(main(["--instance", str(self.root), "downloads", "meta", "Cli.7z"]), 2)

    def test_audit_reports_missing_meta_missing_source_orphans_and_missing_archives(self):
        self.archive("NoMeta.7z")
        self.archive("NoSource.7z")
        (self.downloads / "NoSource.7z.meta").write_text("[General]\ninstalled=true\nmodID=0\nunknownArchive=true\n")
        self.archive("Direct.7z")
        (self.downloads / "Direct.7z.meta").write_text("[General]\ndirectURL=https://a.test/Direct.7z\n")
        self.archive("Nexus.7z")
        (self.downloads / "Nexus.7z.meta").write_text("[General]\ngameName=SkyrimSE\nmodID=1\nfileID=2\n")
        (self.downloads / "Gone.7z.meta").write_text("[General]\ndirectURL=https://a.test/Gone.7z\n")
        mod = self.root / "mods" / "Base Mod"
        (mod / "meta.ini").write_text("[General]\ninstallationFile=Missing Archive.7z\n")
        (self.root / "mods" / "Patch Mod" / "meta.ini").write_text("[General]\ninstallationFile=direct.7Z\n")
        report = audit_downloads(self.instance)
        self.assertFalse(report["ok"])
        self.assertEqual(report["archives_without_meta"], ["NoMeta.7z"])
        self.assertEqual(report["archives_without_wabbajack_source"], [{"archive": "NoSource.7z", "unknownArchive": True}])
        self.assertEqual(report["orphan_meta"], ["Gone.7z.meta"])
        self.assertEqual(report["mods_missing_archive"], [{"mod": "Base Mod", "installationFile": "Missing Archive.7z"}])
        self.assertEqual(report["sources"], {"directURL": 1, "nexus": 1})
        self.assertIn("Unlisted Mod", report["mods_without_installation_file"])

    def test_audit_cli_exit_code(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--instance", str(self.root), "downloads", "audit"]), 0)
        self.archive("NoMeta.7z")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--instance", str(self.root), "--json", "downloads", "audit"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(buffer.getvalue())["archives_without_meta"], ["NoMeta.7z"])


if __name__ == "__main__":
    unittest.main()
