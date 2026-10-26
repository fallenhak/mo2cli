import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from mo2cli import toolwrap
from mo2cli.cli import main
from mo2cli.journal import undo_last
from mo2cli.workspace import Instance, Mo2Error


class ToolWrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "inst"
        self.game = base / "Stock Game"
        (self.game / "Data").mkdir(parents=True)
        (self.game / "Data" / "Skyrim.esm").write_bytes(b"x")
        profile = self.root / "profiles" / "Default"
        profile.mkdir(parents=True)
        for mod, plugin in (("ModA", "A.esp"), ("ModB", "B.esp")):
            (self.root / "mods" / mod).mkdir(parents=True)
            (self.root / "mods" / mod / plugin).write_bytes(mod.encode())
        (profile / "modlist.txt").write_text("+ModB\n+ModA\n", encoding="utf-8")
        (profile / "plugins.txt").write_text("*A.esp\n*B.esp\n", encoding="utf-8")
        (profile / "loadorder.txt").write_text("Skyrim.esm\nA.esp\nB.esp\n", encoding="utf-8")
        (self.root / "ModOrganizer.ini").write_text(f"[General]\ngameName=Skyrim Special Edition\nselected_profile=Default\ngamePath=@ByteArray({self.game.as_posix()})\n", encoding="utf-8")
        self.instance = Instance.open(self.root)
        self.exe = base / "tool.exe"
        self.exe.write_bytes(b"MZ")

    def tearDown(self):
        self.temp.cleanup()

    def loadorder(self):
        return (self.root / "profiles" / "Default" / "loadorder.txt").read_text(encoding="utf-8").split()

    def test_parse_xedit_log(self):
        text = "[00:01] Removing: [WEAP:00012345]\n[00:01] Removing: [ARMO:00012346]\n[00:02] Undeleting: [REFR:00000001]\nSkipping: [NAVM:2]\n[00:03] Done: cleaning\n"
        parsed = toolwrap.parse_xedit_log(text)
        self.assertEqual((parsed["itm"], parsed["udr"], parsed["skipped"], parsed["source"]), (2, 1, 1, "lines"))
        summary = toolwrap.parse_xedit_log("Removed Records: 7\nUndeleted Records: 3\n")
        self.assertEqual((summary["itm"], summary["udr"], summary["source"]), (7, 3, "summary"))

    def fake_loot(self, order, report=None, returncode=0):
        def run(command, **kwargs):
            self.last_command = command
            if returncode == 0:
                Path(command[command.index("--pluginListOutputPath") + 1]).write_text("\n".join(order) + "\n", encoding="utf-8")
                Path(command[command.index("--out") + 1]).write_text(json.dumps(report or {}), encoding="utf-8")
            return subprocess.CompletedProcess(command, returncode, "", "boom")
        return run

    def test_loot_sort_does_not_touch_loadorder_without_apply(self):
        report = {"messages": [{"type": "say", "text": "hi"}], "plugins": [{"name": "A.esp", "dirty": [{"cleaningUtility": "SSEEdit", "itm": 3, "deletedReferences": 1, "deletedNavmesh": 0}], "missingMasters": ["X.esm"], "messages": []}]}
        with patch("mo2cli.toolwrap.subprocess.run", side_effect=self.fake_loot(["Skyrim.esm", "B.esp", "A.esp"], report)):
            result = toolwrap.loot_sort(self.instance, None, exe=str(self.exe))
        self.assertEqual(self.loadorder(), ["Skyrim.esm", "A.esp", "B.esp"])
        self.assertTrue(result["order_changed"])
        self.assertFalse(result["applied"])
        self.assertEqual(result["dirty"][0]["itm"], 3)
        self.assertEqual(result["missing_masters"], [{"plugin": "A.esp", "master": "X.esm"}])
        command = self.last_command
        self.assertEqual(command[command.index("--game") + 1], "SkyrimSE")
        self.assertIn("--skipUpdateMasterlist", command)
        self.assertNotEqual(command[command.index("--pluginListOutputPath") + 1], command[command.index("--pluginListPath") + 1])

    def test_loot_sort_apply_and_undo(self):
        with patch("mo2cli.toolwrap.subprocess.run", side_effect=self.fake_loot(["Skyrim.esm", "B.esp", "A.esp"])):
            result = toolwrap.loot_sort(self.instance, None, exe=str(self.exe), apply=True)
        self.assertTrue(result["applied"])
        self.assertEqual(self.loadorder(), ["Skyrim.esm", "B.esp", "A.esp"])
        self.assertEqual((self.root / "profiles" / "Default" / "plugins.txt").read_text(encoding="utf-8").split(), ["*B.esp", "*A.esp"])
        undo_last(self.instance)
        self.assertEqual(self.loadorder(), ["Skyrim.esm", "A.esp", "B.esp"])
        self.assertEqual((self.root / "profiles" / "Default" / "plugins.txt").read_text(encoding="utf-8").split(), ["*A.esp", "*B.esp"])

    def test_loot_sort_rejects_changed_plugin_set_and_failures(self):
        with patch("mo2cli.toolwrap.subprocess.run", side_effect=self.fake_loot(["Skyrim.esm", "A.esp"])):
            with self.assertRaises(Mo2Error):
                toolwrap.loot_sort(self.instance, None, exe=str(self.exe), apply=True)
        self.assertEqual(self.loadorder(), ["Skyrim.esm", "A.esp", "B.esp"])
        with patch("mo2cli.toolwrap.subprocess.run", side_effect=self.fake_loot([], returncode=1)):
            with self.assertRaises(Mo2Error):
                toolwrap.loot_sort(self.instance, None, exe=str(self.exe))

    def test_loot_sort_dry_run_does_not_start_process(self):
        with patch("mo2cli.toolwrap.subprocess.run", side_effect=AssertionError("must not run")):
            result = toolwrap.loot_sort(self.instance, None, exe=str(self.exe), dry_run=True)
        self.assertTrue(result["dry_run"])

    def test_xedit_clean_runs_in_vfs_parses_log_and_captures(self):
        logs = self.root / "logs"

        def fake_vfs(instance, profile, binary, args, usvfs_dir, destination, direct_mods=False, **kwargs):
            self.vfs_args = (binary, args, destination, direct_mods)
            logs.mkdir(parents=True, exist_ok=True)
            (logs / "SSEEdit_log.txt").write_text("Removing: [WEAP:1]\nUndeleting: [REFR:2]\nDone\n", encoding="utf-8")
            (instance.overwrite_dir / "A.esp").parent.mkdir(parents=True, exist_ok=True)
            (instance.overwrite_dir / "A.esp").write_bytes(b"cleaned")
            return {"returncode": 0}

        with patch("mo2cli.toolwrap.run_vfs", side_effect=fake_vfs):
            result = toolwrap.xedit_clean(self.instance, None, "A.esp", exe=str(self.exe), log_dir=str(logs), capture_to="Cleaned Plugins")
        binary, args, destination, direct = self.vfs_args
        for flag in ("-SSE", "-qac", "-autoexit", "-autoload", "-IKnowWhatImDoing"):
            self.assertIn(flag, args)
        self.assertEqual(args[-1], "A.esp")
        self.assertTrue(any(item.startswith("-P:") and item.endswith("plugins.txt") for item in args))
        self.assertTrue(any(item.startswith("-D:") and item.endswith("Data") for item in args))
        self.assertTrue(direct)
        self.assertTrue(result["ok"])
        self.assertEqual((result["itm"], result["udr"]), (1, 1))
        self.assertEqual(result["capture"]["moved"], ["A.esp"])
        self.assertEqual((self.root / "mods" / "Cleaned Plugins" / "A.esp").read_bytes(), b"cleaned")
        self.assertFalse((self.root / "overwrite" / "A.esp").exists())
        self.assertTrue(Path(result["backup"]).is_file())
        undo_last(self.instance)
        self.assertEqual((self.root / "overwrite" / "A.esp").read_bytes(), b"cleaned")
        self.assertFalse((self.root / "mods" / "Cleaned Plugins" / "A.esp").exists())

    def test_xedit_clean_guards(self):
        with self.assertRaises(Mo2Error):
            toolwrap.xedit_clean(self.instance, None, "Missing.esp", exe=str(self.exe))
        with self.assertRaises(Mo2Error):
            toolwrap.xedit_clean(self.instance, None, "Skyrim.esm", exe=str(self.exe))
        with patch("mo2cli.toolwrap.run_vfs", side_effect=AssertionError("must not run")):
            plan = toolwrap.xedit_clean(self.instance, None, "A.esp", exe=str(self.exe), dry_run=True)
        self.assertIn("-qac", plan["arguments"])

    def test_registered_tool_is_found_by_title(self):
        with (self.root / "ModOrganizer.ini").open("a", encoding="utf-8") as ini:
            ini.write(f"[customExecutables]\n1\\title=SSEEdit\n1\\binary={self.exe.as_posix()}\n")
        self.assertEqual(toolwrap.find_tool(Instance.open(self.root), None, toolwrap.XEDIT_NAMES), self.exe.resolve())

    def test_bsarch_commands(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "Archive: x.bsa\nmeshes\\a.nif\ntextures\\b.dds\n", "")

        with patch("mo2cli.toolwrap.subprocess.run", side_effect=run):
            packed = toolwrap.bsarch(self.instance, None, "pack", "src", str(self.root / "new.bsa"), exe=str(self.exe), filters=["*.nif", "*.dds"])
            unpacked = toolwrap.bsarch(self.instance, None, "unpack", "x.bsa", "out", exe=str(self.exe), compress=False)
            listed = toolwrap.bsarch(self.instance, None, "list", "x.bsa", exe=str(self.exe))
        self.assertEqual(calls[0][1:], ["pack", "src", str(self.root / "new.bsa"), "-sse", "-z", "-mt:yes", "-f:*.nif,*.dds"])
        self.assertEqual(calls[1][1:], ["unpack", "x.bsa", "out", "-mt:yes"])
        self.assertEqual(calls[2][1:], ["x.bsa", "-list"])
        self.assertEqual(listed["files"], ["meshes/a.nif", "textures/b.dds"])
        self.assertEqual(packed["returncode"], 0)
        self.assertEqual(unpacked["action"], "unpack")
        (self.root / "exists.bsa").write_bytes(b"x")
        with patch("mo2cli.toolwrap.subprocess.run", side_effect=run):
            with self.assertRaises(Mo2Error):
                toolwrap.bsarch(self.instance, None, "pack", "src", str(self.root / "exists.bsa"), exe=str(self.exe))

    def test_cli_dry_runs_need_no_lock(self):
        with patch("mo2cli.safety._running_process_names", return_value={"modorganizer.exe"}):
            for argv in (
                ["tools", "xedit-clean", "A.esp", "--exe", str(self.exe), "--dry-run"],
                ["tools", "loot-sort", "--exe", str(self.exe), "--apply", "--dry-run"],
                ["tools", "bsarch", "list", "x.bsa", "--exe", str(self.exe), "--dry-run"],
            ):
                out = StringIO()
                with redirect_stdout(out):
                    code = main(["--instance", str(self.root), "--json", *argv])
                self.assertEqual(code, 0, argv)
                self.assertTrue(json.loads(out.getvalue())["dry_run"])


if __name__ == "__main__":
    unittest.main()
