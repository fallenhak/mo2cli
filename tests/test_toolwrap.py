import json
import shutil
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

    def test_parse_xedit_log_sseedit_415_summary(self):
        text = (
            "[02:08] Done: Applying Filter, [Pass 1] Processed Records: 21234, [Pass 2] Processed Records: 18911\n"
            "[Undeleting and Disabling References done]  Processed Records: 18910, Undeleted Records: 93, Elapsed Time: 00:00\n"
            "[Removing \"Identical to Master\" records done]  Processed Records: 18910, Removed Records: 386, Elapsed Time: 00:00\n"
            "[Undeleting and Disabling References done]  Processed Records: 18524, Undeleted Records: 0, Elapsed Time: 00:00\n"
            "[Removing \"Identical to Master\" records done]  Processed Records: 18524, Removed Records: 0, Elapsed Time: 00:00\n"
        )
        parsed = toolwrap.parse_xedit_log(text)
        self.assertEqual((parsed["itm"], parsed["udr"], parsed["source"]), (386, 93, "summary"))

    def fake_loot(self, order, report=None, returncode=0, in_place=False):
        def run(command, **kwargs):
            self.last_command = command
            if returncode == 0:
                target = "--pluginListPath" if in_place else "--pluginListOutputPath"
                Path(command[command.index(target) + 1]).write_text("\n".join(order) + "\n", encoding="utf-8")
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

    def test_loot_sort_applies_profile_load_rules_after_loot(self):
        (self.root / "profiles" / "Default" / toolwrap.LOAD_RULES_FILE).write_text("# keep A last\nA.esp after B.esp\n", encoding="utf-8")
        with patch("mo2cli.toolwrap.subprocess.run", side_effect=self.fake_loot(["Skyrim.esm", "A.esp", "B.esp"])):
            result = toolwrap.loot_sort(self.instance, None, exe=str(self.exe), apply=True)
        self.assertEqual(result["load_rules"], ["A.esp after B.esp"])
        self.assertEqual(self.loadorder(), ["Skyrim.esm", "B.esp", "A.esp"])

    def test_load_rules_refuse_moving_past_dependents_and_bad_lines(self):
        order = ["Skyrim.esm", "A.esp", "B.esp", "C.esp"]
        self.assertEqual(toolwrap.apply_load_rules(order, [("A.esp", "C.esp")], {})[0], ["Skyrim.esm", "B.esp", "C.esp", "A.esp"])
        self.assertEqual(toolwrap.apply_load_rules(order, [("C.esp", "A.esp"), ("X.esp", "A.esp")], {}), (order, []))
        with self.assertRaises(Mo2Error):
            toolwrap.apply_load_rules(order, [("A.esp", "C.esp")], {"b.esp": ["a.esp"]})
        rules = self.root / "rules.txt"
        rules.write_text("A.esp before B.esp\n", encoding="utf-8")
        with self.assertRaises(Mo2Error):
            toolwrap.read_load_rules(rules)

    def test_loot_sort_rejects_changed_plugin_set_and_failures(self):
        with patch("mo2cli.toolwrap.subprocess.run", side_effect=self.fake_loot(["Skyrim.esm", "A.esp"])):
            with self.assertRaises(Mo2Error):
                toolwrap.loot_sort(self.instance, None, exe=str(self.exe), apply=True)
        self.assertEqual(self.loadorder(), ["Skyrim.esm", "A.esp", "B.esp"])
        with patch("mo2cli.toolwrap.subprocess.run", side_effect=self.fake_loot([], returncode=1)):
            with self.assertRaises(Mo2Error):
                toolwrap.loot_sort(self.instance, None, exe=str(self.exe))

    def test_loot_sort_reads_order_written_back_to_plugin_list_path(self):
        (self.game / "SkyrimSE.exe").write_bytes(b"MZ")
        (self.game / "Skyrim.ccc").write_text("", encoding="utf-8")
        staged = {}

        def run(command, **kwargs):
            game = Path(command[command.index("--gamePath") + 1])
            staged["files"] = sorted(path.name for path in game.rglob("*") if path.is_file())
            staged["plugins_txt"] = (Path(command[command.index("--pluginListPath") + 1]).parent / "plugins.txt").is_file()
            return self.fake_loot(["Skyrim.esm", "B.esp", "A.esp"], in_place=True)(command, **kwargs)

        with patch("mo2cli.toolwrap.subprocess.run", side_effect=run):
            result = toolwrap.loot_sort(self.instance, None, exe=str(self.exe), apply=True)
        self.assertEqual(staged["files"], ["A.esp", "B.esp", "Skyrim.ccc", "Skyrim.esm", "SkyrimSE.exe"])
        self.assertTrue(staged["plugins_txt"])
        self.assertEqual(result["sorted_from"], "loadorder.txt")
        self.assertEqual(self.loadorder(), ["Skyrim.esm", "B.esp", "A.esp"])

    def test_xedit_clean_returns_overwrite_output_to_source_mod(self):
        logs = self.root / "logs"
        source = self.root / "mods" / "ModA" / "A.esp"

        def fake_vfs(instance, profile, binary, args, usvfs_dir, destination, direct_mods=False, **kwargs):
            self.log_arg = next(item for item in args if item.startswith("-R:"))
            Path(self.log_arg[3:]).write_text("Removing: [WEAP:1]\nDone\n", encoding="utf-8")
            backups = instance.overwrite_dir / "SSEEdit Backups"
            backups.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(backups / "A.esp.backup.1"))
            (instance.overwrite_dir / "A.esp").write_bytes(b"cleaned")
            return {"returncode": 0}

        with patch("mo2cli.toolwrap.run_vfs", side_effect=fake_vfs):
            result = toolwrap.xedit_clean(self.instance, None, "A.esp", exe=str(self.exe), log_dir=str(logs), isolated=False)
        self.assertTrue(self.log_arg.endswith(".txt"))
        self.assertTrue(result["ok"])
        self.assertEqual(source.read_bytes(), b"cleaned")
        self.assertEqual(result["cleaned_into"], str(source))
        self.assertFalse((self.root / "overwrite" / "A.esp").exists())
        self.assertFalse((self.root / "overwrite" / "SSEEdit Backups").exists())
        self.assertEqual(Path(result["backup"]).read_bytes(), b"ModA")

    def test_xedit_clean_fails_loudly_when_game_file_changes(self):
        (self.root / "mods" / "ModA" / "Skyrim.esm").write_bytes(b"copy")

        def fake_vfs(instance, profile, binary, args, usvfs_dir, destination, direct_mods=False, **kwargs):
            (self.game / "Data" / "Skyrim.esm").write_bytes(b"cleaned in the wrong place")
            return {"returncode": 0}

        with patch("mo2cli.toolwrap.run_vfs", side_effect=fake_vfs):
            with self.assertRaisesRegex(Mo2Error, "real game file"):
                toolwrap.xedit_clean(self.instance, None, "Skyrim.esm", exe=str(self.exe), log_dir=str(self.root / "logs"), force=True, isolated=False)

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
            result = toolwrap.xedit_clean(self.instance, None, "A.esp", exe=str(self.exe), log_dir=str(logs), capture_to="Cleaned Plugins", isolated=False)
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

    def test_xedit_clean_isolated_cleans_copy_with_masters_only(self):
        source = self.root / "mods" / "ModA" / "A.esp"
        source.write_bytes(b"TES4\x00\x00\x00\x00" + b"\x00" * 16)  # header without masters
        game_master = self.game / "Data" / "Skyrim.esm"
        seen = {}

        def fake_run(command, **kwargs):
            arguments = command[1:]
            data = Path(next(item for item in arguments if item.startswith("-D:"))[3:])
            seen["data"] = sorted(path.name for path in data.iterdir())
            seen["plugins"] = Path(next(item for item in arguments if item.startswith("-P:"))[3:]).read_text(encoding="utf-8").split()
            seen["settings"] = (data.parent / "plugins.sseviewsettings").read_text(encoding="utf-8")
            # xEdit saves a new file and renames the original into a backup folder
            (data / "SSEEdit Backups").mkdir()
            (data / "A.esp").rename(data / "SSEEdit Backups" / "A.esp.backup")
            (data / "A.esp").write_bytes(b"cleaned")
            Path(next(item for item in arguments if item.startswith("-R:"))[3:]).write_text("Removing: [WEAP:1]\nDone\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0)

        with patch("mo2cli.toolwrap.parse_header", side_effect=lambda path: type("H", (), {"masters": ["Skyrim.esm"] if path.name == "A.esp" else []})()):
            with patch("mo2cli.toolwrap.subprocess.run", side_effect=fake_run):
                result = toolwrap.xedit_clean(self.instance, None, "A.esp", exe=str(self.exe), log_dir=str(self.root / "logs"))
        self.assertEqual(seen["data"], ["A.esp", "Skyrim.esm"])
        self.assertEqual(seen["plugins"], ["*Skyrim.esm", "*A.esp"])
        self.assertIn("ShowTip=0", seen["settings"])
        self.assertIn("[DeveloperMessage]", seen["settings"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["cleaned"])
        self.assertEqual(result["itm"], 1)
        self.assertEqual(source.read_bytes(), b"cleaned")
        self.assertEqual(game_master.read_bytes(), b"x")
        self.assertEqual(Path(result["backup"]).read_bytes()[:4], b"TES4")

    def test_xedit_clean_isolated_refuses_game_file_without_capture(self):
        with patch("mo2cli.toolwrap.subprocess.run", side_effect=AssertionError("must not run")):
            with self.assertRaisesRegex(Mo2Error, "not inside a mod"):
                toolwrap.xedit_clean(self.instance, None, "Skyrim.esm", exe=str(self.exe), force=True)

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

    def test_synthesis_run_builds_flags_and_captures(self):
        settings = self.root / "PipelineSettings.json"
        settings.write_text("{}", encoding="utf-8")

        def fake_vfs(instance, profile, binary, args, usvfs_dir, destination, direct_mods=False, **kwargs):
            self.vfs_args = (args, destination, direct_mods)
            (instance.mods_dir / "Synthesis Output" / "Synthesis.esp").write_bytes(b"s")
            instance.overwrite_dir.mkdir(exist_ok=True)
            (instance.overwrite_dir / "stray.txt").write_bytes(b"x")
            return {"returncode": 0}

        with patch("mo2cli.toolwrap.run_vfs", side_effect=fake_vfs):
            result = toolwrap.synthesis_run(self.instance, None, str(settings), exe=str(self.exe), profile_identifier="Main", target_runtime="net8.0", capture_to="Synth Extras")
        args, destination, direct = self.vfs_args
        self.assertEqual(args[0], "run-pipeline")
        self.assertEqual(args[args.index("-o") + 1], str(self.root / "mods" / "Synthesis Output"))
        self.assertEqual(args[args.index("-s") + 1], str(settings.resolve()))
        self.assertEqual(args[args.index("-p") + 1], "Main")
        self.assertTrue(args[args.index("-l") + 1].endswith("plugins.txt"))
        self.assertTrue(args[args.index("-d") + 1].endswith("Data"))
        self.assertEqual(args[args.index("-t") + 1], "net8.0")
        self.assertNotIn("-e", args)
        self.assertTrue(direct)
        self.assertTrue(result["ok"])
        self.assertEqual(result["output_files"], 1)
        self.assertEqual(result["capture"]["moved"], ["stray.txt"])
        self.assertIn("Synthesis Output", (self.root / "profiles" / "Default" / "modlist.txt").read_text(encoding="utf-8"))
        with self.assertRaises(Mo2Error):
            toolwrap.synthesis_run(self.instance, None, str(self.root / "missing.json"), exe=str(self.exe))
        with patch("mo2cli.toolwrap.run_vfs", side_effect=AssertionError("must not run")):
            plan = toolwrap.synthesis_run(self.instance, None, str(settings), exe=str(self.exe), extra_data="X", dry_run=True)
        self.assertIn("-e", plan["arguments"])

    def test_pandora_run_flags(self):
        def fake_vfs(instance, profile, binary, args, usvfs_dir, destination, direct_mods=False, **kwargs):
            self.vfs_args = (args, destination)
            return {"returncode": 3}

        with patch("mo2cli.toolwrap.run_vfs", side_effect=fake_vfs):
            result = toolwrap.pandora_run(self.instance, None, exe=str(self.exe))
        args, destination = self.vfs_args
        self.assertEqual(args, ["--tesv", str(self.game), "-o", str(self.root / "mods" / "Pandora Output"), "--auto_run", "--auto_close"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["active_mods_created"], [])
        plan = toolwrap.pandora_run(self.instance, None, exe=str(self.exe), auto_run=False, auto_close=False, dry_run=True)
        self.assertEqual(plan["arguments"], ["--tesv", str(self.game), "-o", str(self.root / "mods" / "Pandora Output")])

    def test_pandora_run_seeds_active_mods_and_captures_overwrite(self):
        (self.root / "mods" / "ModA" / "Nemesis_Engine" / "mod" / "colis").mkdir(parents=True)
        (self.root / "mods" / "ModA" / "Nemesis_Engine" / "mod" / "colis" / "info.ini").write_text("name=Precision\n", encoding="utf-8")
        (self.root / "mods" / "ModB" / "Nemesis_Engine" / "mod" / "nofile").mkdir(parents=True)
        (self.game / "Data" / "Pandora_Engine" / "mod" / "pandora").mkdir(parents=True)
        (self.game / "Data" / "Pandora_Engine" / "mod" / "pandora" / "info.xml").write_text("<x/>", encoding="utf-8")
        (self.root / "mods" / "ModB" / "Nemesis_Engine" / "mod" / "aaa").mkdir(parents=True)
        (self.root / "mods" / "ModB" / "Nemesis_Engine" / "mod" / "aaa" / "info.ini").write_text("name=A\n", encoding="utf-8")
        self.assertEqual(toolwrap.pandora_mod_codes(self.instance, None), ["aaa", "colis", "pandora"])

        def fake_vfs(instance, profile, binary, args, usvfs_dir, destination, direct_mods=False, **kwargs):
            overwrite = self.root / "overwrite"
            (overwrite / "meshes").mkdir(parents=True, exist_ok=True)
            (overwrite / "meshes" / "behavior.hkx").write_bytes(b"hkx")
            (overwrite / "Engine.log").write_text("log", encoding="utf-8")
            return {"returncode": 0}

        with patch("mo2cli.toolwrap.run_vfs", side_effect=fake_vfs):
            result = toolwrap.pandora_run(self.instance, None, exe=str(self.exe))
        saved = json.loads((self.exe.parent / "Pandora_Engine" / "ActiveMods.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, [{"code": "aaa", "active": True, "priority": 1}, {"code": "colis", "active": True, "priority": 2}, {"code": "pandora", "active": True, "priority": 3}])
        self.assertEqual(result["capture"]["moved"], ["meshes/behavior.hkx"])
        self.assertTrue((self.root / "mods" / "Pandora Output" / "meshes" / "behavior.hkx").is_file())
        self.assertTrue((self.root / "overwrite" / "Engine.log").is_file())

    def test_pandora_run_adds_new_codes_active_to_saved_cache(self):
        for code in ("colis", "newanim"):
            (self.root / "mods" / "ModA" / "Nemesis_Engine" / "mod" / code).mkdir(parents=True)
            (self.root / "mods" / "ModA" / "Nemesis_Engine" / "mod" / code / "info.ini").write_text("name=x\n", encoding="utf-8")
        cache = self.exe.parent / "Pandora_Engine" / "ActiveMods.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps([{"code": "colis", "active": False, "priority": 4}]), encoding="utf-8")
        plan = toolwrap.pandora_run(self.instance, None, exe=str(self.exe), dry_run=True)
        self.assertEqual(plan["active_mods_added"], ["newanim"])
        self.assertEqual(json.loads(cache.read_text(encoding="utf-8")), [{"code": "colis", "active": False, "priority": 4}])
        with patch("mo2cli.toolwrap.run_vfs", return_value={"returncode": 0}):
            result = toolwrap.pandora_run(self.instance, None, exe=str(self.exe))
        self.assertEqual(result["active_mods_added"], ["newanim"])
        self.assertEqual(
            json.loads(cache.read_text(encoding="utf-8")),
            [{"code": "colis", "active": False, "priority": 4}, {"code": "newanim", "active": True, "priority": 5}],
        )

    def test_cli_dry_runs_need_no_lock(self):
        with patch("mo2cli.safety._running_process_names", return_value={"modorganizer.exe"}):
            for argv in (
                ["tools", "xedit-clean", "A.esp", "--exe", str(self.exe), "--dry-run"],
                ["tools", "loot-sort", "--exe", str(self.exe), "--apply", "--dry-run"],
                ["tools", "bsarch", "list", "x.bsa", "--exe", str(self.exe), "--dry-run"],
                ["tools", "pandora-run", "--exe", str(self.exe), "--dry-run"],
            ):
                out = StringIO()
                with redirect_stdout(out):
                    code = main(["--instance", str(self.root), "--json", *argv])
                self.assertEqual(code, 0, argv)
                self.assertTrue(json.loads(out.getvalue())["dry_run"])


if __name__ == "__main__":
    unittest.main()
