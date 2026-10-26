import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from mo2cli import research
from mo2cli.cli import main
from mo2cli.workspace import Mo2Error

PAYLOAD = {
    "collectionRevision": {
        "revisionNumber": 2,
        "modCount": 2,
        "collection": {"name": "LoreRim", "slug": "bsyabf"},
        "modFiles": [
            {"optional": False, "file": {"fileId": 595880, "name": "FOMOD Plus", "version": "1.11.0", "mod": {"modId": 141001, "name": "FOMOD Plus", "game": {"domainName": "skyrimspecialedition"}}}},
            {"optional": True, "file": {"fileId": 5, "name": "Opt", "version": "1", "mod": {"modId": 7, "name": "Optional Thing", "game": {"domainName": "skyrimspecialedition"}}}},
            {"optional": False, "file": None},
        ],
    }
}


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def _graphql(self, query, variables):
        self.calls.append((query, variables))
        return self.payload


class CollectionTests(unittest.TestCase):
    def test_collection_shape(self):
        client = FakeClient(PAYLOAD)
        result = research.collection("bsyabf", 2, client=client)
        self.assertEqual(client.calls[0][1], {"slug": "bsyabf", "revision": 2})
        self.assertEqual((result["revision"], result["listed"], result["optional"]), (2, 2, 1))
        self.assertEqual(result["mods"][0], {"name": "FOMOD Plus", "modId": 141001, "fileId": 595880, "fileName": "FOMOD Plus", "version": "1.11.0", "optional": False, "game": "skyrimspecialedition"})
        self.assertTrue(result["mods"][1]["optional"])

    def test_missing_collection(self):
        with self.assertRaises(Mo2Error):
            research.collection("nope", client=FakeClient({"collectionRevision": None}))

    def test_cli_json(self):
        out = StringIO()
        with patch("mo2cli.research.NexusClient", return_value=FakeClient(PAYLOAD)), redirect_stdout(out):
            code = main(["--json", "research", "collection", "bsyabf", "--revision", "2"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["mods"][0]["modId"], 141001)


LOL = {"data": {"name": "LoreRim", "version": "5.0.2", "slug": "lorerim", "author": {"name": "biggie_boss"}, "game": {"name": "TESV Skyrim SE"}, "updated": "2026-06-02",
                "files": [{"name": "abc-modlist.txt", "clean_name": "modlist.txt", "size_in_bytes": 10, "content": ["+A", "-B"]},
                          {"name": "def-plugins.txt", "clean_name": "plugins.txt", "size_in_bytes": 7, "content": ["*A.esp"]}]}}


class LolTests(unittest.TestCase):
    def test_lol_summary_and_save(self):
        with tempfile.TemporaryDirectory() as temp, patch("mo2cli.research._http_json", return_value=LOL) as http:
            result = research.lol("lorerim", save_to=temp, include_content=True)
            self.assertTrue(http.call_args[0][0].endswith("/v1/lists/lorerim"))
            self.assertEqual([item["name"] for item in result["files"]], ["modlist.txt", "plugins.txt"])
            self.assertEqual(result["files"][0]["content"], ["+A", "-B"])
            self.assertEqual((Path(temp) / "modlist.txt").read_bytes(), b"+A" + bytes([13, 10]) + b"-B" + bytes([13, 10]))
        self.assertEqual(result["author"], "biggie_boss")

    def test_lol_not_found(self):
        error = urllib.error.HTTPError("u", 404, "nf", {}, None)
        with patch("mo2cli.research.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(Mo2Error):
                research.lol("zzz")

    def test_cli(self):
        out = StringIO()
        with patch("mo2cli.research._http_json", return_value=LOL), redirect_stdout(out):
            code = main(["--json", "research", "lol", "lorerim"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["files"][1]["lines"], 1)
