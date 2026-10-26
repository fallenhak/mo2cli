import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch

from mo2cli import github
from mo2cli.cli import main
from mo2cli.metadata import IniDocument
from mo2cli.workspace import Instance, Mo2Error

RELEASE = {
    "tag_name": "v1.2",
    "assets": [
        {"name": "Tool-1.2.7z", "size": 5, "browser_download_url": "https://github.com/o/r/releases/download/v1.2/Tool-1.2.7z"},
        {"name": "Tool-1.2-src.zip", "size": 6, "browser_download_url": "https://github.com/o/r/releases/download/v1.2/Tool-1.2-src.zip"},
    ],
}


class Response(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def fake_urlopen(log):
    def opener(request, timeout=0):
        log.append(request.full_url)
        if "api.github.com" in request.full_url:
            return Response(json.dumps(RELEASE).encode())
        return Response(b"archive-bytes")

    return opener


class GithubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "profiles" / "Default").mkdir(parents=True)
        (self.root / "downloads").mkdir()
        (self.root / "ModOrganizer.ini").write_text("[General]\nselected_profile=Default\n", encoding="utf-8")
        self.instance = Instance.open(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_parse_ref(self):
        self.assertEqual(github.parse_ref("github:o/r"), ("o", "r", "latest"))
        self.assertEqual(github.parse_ref("github:o/r@v1.0"), ("o", "r", "v1.0"))
        self.assertTrue(github.is_github_ref("GitHub:o/r"))
        with self.assertRaises(Mo2Error):
            github.parse_ref("github:nonsense")

    def test_resolve_latest_and_tag_endpoints(self):
        log = []
        with patch("mo2cli.github.urllib.request.urlopen", side_effect=fake_urlopen(log)):
            github.resolve_asset("github:o/r@latest", "*.7z")
            github.resolve_asset("github:o/r@v1.2", "*.7z")
        self.assertEqual(log, ["https://api.github.com/repos/o/r/releases/latest", "https://api.github.com/repos/o/r/releases/tags/v1.2"])

    def test_asset_selection_errors(self):
        with patch("mo2cli.github.urllib.request.urlopen", side_effect=fake_urlopen([])):
            with self.assertRaises(Mo2Error):
                github.resolve_asset("github:o/r")  # two assets, no glob
            with self.assertRaises(Mo2Error):
                github.resolve_asset("github:o/r", "*.rar")
            self.assertEqual(github.resolve_asset("github:o/r", "tool-*.7Z")["asset"], "Tool-1.2.7z")

    def test_fetch_writes_direct_url_meta(self):
        with patch("mo2cli.github.urllib.request.urlopen", side_effect=fake_urlopen([])), patch("mo2cli.downloads.urllib.request.urlopen", side_effect=fake_urlopen([])):
            result = github.fetch_asset(self.instance, "github:o/r@latest", "*.7z")
        archive = self.root / "downloads" / "Tool-1.2.7z"
        self.assertEqual(archive.read_bytes(), b"archive-bytes")
        meta = IniDocument.read(archive.with_name(archive.name + ".meta"))
        self.assertEqual(meta.get("directURL", section="General"), RELEASE["assets"][0]["browser_download_url"])
        self.assertEqual(result["github"]["tag"], "v1.2")

    def test_cli_fetch_and_install_dry_run(self):
        out = StringIO()
        with patch("mo2cli.github.urllib.request.urlopen", side_effect=fake_urlopen([])), patch("mo2cli.downloads.urllib.request.urlopen", side_effect=fake_urlopen([])), patch("mo2cli.safety._running_process_names", return_value=set()), redirect_stdout(out):
            code = main(["--instance", str(self.root), "--json", "downloads", "fetch", "github:o/r@latest", "--asset", "*.7z"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["github"]["asset"], "Tool-1.2.7z")
        with patch("sys.stderr", new=StringIO()):
            code = main(["--instance", str(self.root), "mods", "install", "github:o/r@latest", "--dry-run"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
