import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
import tempfile
from pathlib import Path
from unittest.mock import patch

from mo2cli.cli import main
from mo2cli.nexus import GRAPHQL_URL, NexusClient
from mo2cli.workspace import Mo2Error


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


GRAPHQL_REPLY = {"data": {"modFiles": [
    {"fileId": 5, "name": "Main", "version": "1.0", "category": "OLD_VERSION", "date": 1, "size": 10, "uri": "a.7z"},
    {"fileId": 6, "name": "Main 2", "version": "2.0", "category": "MAIN", "date": 2, "size": 11, "uri": "b.7z"},
]}}


class NexusFilesTests(unittest.TestCase):
    def test_files_all_posts_graphql_and_normalizes(self):
        seen = {}

        def fake(request, timeout=0):
            seen["url"] = request.full_url
            seen["body"] = json.loads(request.data)
            seen["headers"] = {key.lower(): value for key, value in request.header_items()}
            return FakeResponse(GRAPHQL_REPLY)

        with patch("mo2cli.nexus.urllib.request.urlopen", side_effect=fake):
            files = NexusClient("KEY").files_all("skyrimspecialedition", 123)
        self.assertEqual(seen["url"], GRAPHQL_URL)
        self.assertEqual(seen["body"]["variables"], {"modId": "123", "gameId": "1704"})
        self.assertIn("modFiles", seen["body"]["query"])
        self.assertEqual(seen["headers"]["apikey"], "KEY")
        self.assertTrue(seen["headers"]["user-agent"].startswith("mo2cli/"))
        self.assertEqual([item["category_name"] for item in files], ["OLD_VERSION", "MAIN"])
        self.assertEqual(files[0]["file_id"], 5)

    def test_graphql_errors_and_unknown_game(self):
        with patch("mo2cli.nexus.urllib.request.urlopen", return_value=FakeResponse({"errors": [{"message": "bad"}]})):
            with self.assertRaises(Mo2Error):
                NexusClient("KEY").files_all("skyrimspecialedition", 1)
        with self.assertRaises(Mo2Error):
            NexusClient("KEY").files_all("oblivion", 1)

    def test_cli_all_and_default_category_column(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "profiles" / "Default").mkdir(parents=True)
            (root / "ModOrganizer.ini").write_text("[General]\ngameName=Skyrim Special Edition\nselected_profile=Default\n", encoding="utf-8")
            out = StringIO()
            with patch("mo2cli.nexus.urllib.request.urlopen", return_value=FakeResponse(GRAPHQL_REPLY)), redirect_stdout(out):
                code = main(["--instance", str(root), "nexus", "files", "123", "--all", "--nexus-api-key", "K"])
            self.assertEqual(code, 0)
            self.assertIn("category=OLD_VERSION file_id=5", out.getvalue())
            v1 = [{"file_id": 1, "name": "x", "file_name": "x.7z", "version": "1", "category_name": "MAIN"}]
            out = StringIO()
            with patch("mo2cli.nexus.urllib.request.urlopen", return_value=FakeResponse({"files": v1})), redirect_stdout(out):
                main(["--instance", str(root), "nexus", "files", "123", "--nexus-api-key", "K"])
            self.assertIn("category=MAIN file_id=1", out.getvalue())


if __name__ == "__main__":
    unittest.main()
