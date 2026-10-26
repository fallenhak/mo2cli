import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import py7zr

from mo2cli import qsettings
from mo2cli.cli import main
from mo2cli.instance import detect_game_edition, initialize_portable, render_mo2_ini
from mo2cli.metadata import IniDocument
from mo2cli.workspace import Instance, Mo2Error


class QSettingsCodecTests(unittest.TestCase):
    def test_decode_byte_array_with_doubled_backslashes(self):
        self.assertEqual(qsettings.decode(r"@ByteArray(C:\\Games\\Skyrim Special Edition)"), r"C:\Games\Skyrim Special Edition")
        self.assertEqual(qsettings.decode("@ByteArray(Default)"), "Default")

    def test_decode_quotes_and_escaped_quotes_and_plain_values(self):
        self.assertEqual(qsettings.decode('"a\\"b"'), 'a"b')
        self.assertEqual(qsettings.decode("C:/Games/Skyrim"), "C:/Games/Skyrim")
        self.assertEqual(qsettings.decode(r"C:\Games\Skyrim"), r"C:\Games\Skyrim")  # legacy single backslashes are left alone
        self.assertEqual(qsettings.decode("%BASE_DIR%/mods"), "%BASE_DIR%/mods")

    def test_utf8_bytes_round_trip(self):
        encoded = qsettings.encode_byte_array(r"C:\Users\Kerim\Oyun\Skyrim \u00dc".replace(r"\u00dc", "\u00dc"))
        self.assertIn(r"\xc3\x9c", encoded)
        self.assertEqual(qsettings.decode(encoded), "C:\\Users\\Kerim\\Oyun\\Skyrim \u00dc")

    def test_encode_doubles_backslashes_and_quotes_special_characters(self):
        self.assertEqual(qsettings.encode_byte_array(r"C:\a b"), r"@ByteArray(C:\\a b)")
        self.assertEqual(qsettings.encode("x;y"), '"x;y"')
        self.assertEqual(qsettings.encode(r"a\b"), r"a\\b")


def make_mo2_zip(path: Path, wrapper: str | None = None) -> Path:
    prefix = f"{wrapper}/" if wrapper else ""
    with zipfile.ZipFile(path, "w") as output:
        output.writestr(prefix + "ModOrganizer.exe", b"exe")
        output.writestr(prefix + "plugins/basic_games/__init__.py", b"")
        output.writestr(prefix + "dlls/dll.txt", b"1")
    return path


class PortableInstanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.archive = make_mo2_zip(self.root / "Mod.Organizer-2.5.2.zip")
        self.target = self.root / "Modlists" / "Test"
        self.game = self.target / "Stock Game"
        self.my_games = self.root / "MyGames"

    def tearDown(self):
        self.temp.cleanup()

    def init(self, **kwargs):
        self.game.mkdir(parents=True, exist_ok=True)
        options = {"profile": "Default", "my_games": self.my_games}
        options.update(kwargs)
        return initialize_portable(self.target, self.archive, "Skyrim Special Edition", self.game, **options)

    def test_creates_portable_layout(self):
        result = self.init()
        for relative in ("ModOrganizer.exe", "portable.txt", "plugins/basic_games/__init__.py", "dlls/dll.txt"):
            self.assertTrue((self.target / relative).is_file(), relative)
        for directory in ("downloads", "mods", "overwrite", "profiles/Default"):
            self.assertTrue((self.target / directory).is_dir(), directory)
        self.assertEqual((self.target / "portable.txt").read_bytes(), b"")
        self.assertEqual(result["mo2_version"], "2.5.2")
        self.assertTrue(result["portable"])
        self.assertIsNone(result["strip_root"])

    def test_wrapped_archive_is_flattened(self):
        self.archive = make_mo2_zip(self.root / "Mod.Organizer-2.5.2.zip", wrapper="Mod Organizer 2")
        result = self.init()
        self.assertEqual(result["strip_root"], "Mod Organizer 2")
        self.assertTrue((self.target / "ModOrganizer.exe").is_file())
        self.assertFalse((self.target / "Mod Organizer 2").exists())

    def test_seven_zip_archive(self):
        source = self.root / "src"
        source.mkdir()
        (source / "ModOrganizer.exe").write_bytes(b"exe")
        archive = self.root / "Mod.Organizer-2.5.2.7z"
        with py7zr.SevenZipFile(archive, "w") as output:
            output.write(source / "ModOrganizer.exe", "ModOrganizer.exe")
        self.archive = archive
        self.init()
        self.assertTrue((self.target / "ModOrganizer.exe").is_file())
        self.assertTrue((self.target / "downloads" / "Mod.Organizer-2.5.2.7z").is_file())

    def test_modorganizer_ini_matches_mo2_format_exactly(self):
        self.init()
        raw = (self.target / "ModOrganizer.ini").read_bytes().decode("utf-8")
        game = str(self.game.resolve()).replace("\\", "\\\\")
        expected = "\r\n".join([
            "[General]",
            "gameName=Skyrim Special Edition",
            f"gamePath=@ByteArray({game})",
            "game_edition=Steam",
            "selected_profile=@ByteArray(Default)",
            "version=2.5.2",
            "first_start=false",
            "",
            "[Settings]",
            "profile_local_inis=true",
            "profile_local_saves=true",
            "profile_archive_invalidation=false",
            "",
        ])
        self.assertEqual(raw, expected)
        self.assertNotIn("base_directory", raw)  # portable: MO2 omits path settings equal to the defaults

    def test_instance_open_decodes_byte_array_game_path_and_profile(self):
        self.init()
        instance = Instance.open(self.target)
        self.assertEqual(instance.game_path, self.game.resolve())
        self.assertEqual(instance.selected_profile, "Default")
        self.assertEqual(instance.game_name, "Skyrim Special Edition")
        self.assertEqual(instance.base, self.target.resolve())
        self.assertEqual(instance.downloads_dir, self.target.resolve() / "downloads")
        self.assertEqual(instance.mods_dir, self.target.resolve() / "mods")
        self.assertEqual(instance.list_profiles(), ["Default"])

    def test_profile_files_and_settings(self):
        self.init()
        profile = self.target / "profiles" / "Default"
        header = "# This file was automatically generated by Mod Organizer.\r\n"
        self.assertEqual((profile / "modlist.txt").read_bytes().decode(), header)
        self.assertEqual((profile / "plugins.txt").read_bytes().decode(), header)
        self.assertEqual((profile / "loadorder.txt").read_bytes().decode(), header)
        self.assertTrue((profile / "archives.txt").is_file())
        document = IniDocument.read(profile / "settings.ini")
        self.assertIs(document.get("LocalSaves", section="General"), True)
        self.assertIs(document.get("LocalSettings", section="General"), True)
        settings = Instance.open(self.target).profile_settings("Default")
        self.assertEqual(settings, {"LocalSettings": True, "LocalSaves": True})
        Instance.open(self.target).set_profile_settings("Default", local_saves=False)
        self.assertEqual(Instance.open(self.target).profile_settings("Default")["LocalSaves"], False)
        self.assertEqual(IniDocument.read(profile / "settings.ini").lines.count("[General]"), 1)

    def test_profile_inis_come_from_game_defaults_and_my_games(self):
        self.game.mkdir(parents=True)
        (self.game / "Skyrim_Default.ini").write_text("[General]\nsLanguage=ENGLISH\n")
        self.my_games.mkdir()
        (self.my_games / "Skyrim.ini").write_text("[General]\nuser=1\n")
        (self.my_games / "SkyrimPrefs.ini").write_text("[Display]\nfoo=1\n")
        result = self.init()
        profile = self.target / "profiles" / "Default"
        self.assertEqual(sorted(result["profile_inis"]), ["skyrim.ini", "skyrimprefs.ini"])
        self.assertIn("sLanguage", (profile / "skyrim.ini").read_text())
        self.assertIn("foo=1", (profile / "skyrimprefs.ini").read_text())
        self.assertFalse((profile / "skyrimcustom.ini").exists())

    def test_profile_ini_falls_back_to_my_games_skyrim_ini(self):
        self.my_games.mkdir()
        (self.my_games / "Skyrim.ini").write_text("[General]\nuser=1\n")
        result = self.init()
        self.assertEqual(result["profile_inis"], ["skyrim.ini"])

    def test_mo2_archive_is_copied_to_downloads_with_meta(self):
        self.init(mo2_url="https://github.com/ModOrganizer2/modorganizer/releases/download/v2.5.2/Mod.Organizer-2.5.2.7z")
        archive = self.target / "downloads" / "Mod.Organizer-2.5.2.zip"
        self.assertEqual(archive.read_bytes(), self.archive.read_bytes())
        values = IniDocument.read(Path(str(archive) + ".meta")).as_dict("General")
        self.assertEqual(values["directURL"], "https://github.com/ModOrganizer2/modorganizer/releases/download/v2.5.2/Mod.Organizer-2.5.2.7z")
        self.assertIs(values["installed"], True)
        self.assertIs(values["uninstalled"], False)
        self.assertIs(values["removed"], False)
        self.assertEqual(values["name"], "Mod.Organizer-2.5.2.zip")
        self.assertEqual(values["version"], "2.5.2")

    def test_without_url_meta_has_no_source_and_result_explains(self):
        result = self.init()
        values = IniDocument.read(Path(result["meta"])).as_dict("General")
        self.assertNotIn("directURL", values)
        self.assertIn("downloads meta", result["note"])

    def test_version_falls_back_to_executable_file_version(self):
        self.archive = make_mo2_zip(self.root / "mo2.zip")
        with patch("mo2cli.instance._windows_file_version", return_value="2.5.2.0"):
            result = self.init()
        self.assertEqual(result["mo2_version"], "2.5.2")
        self.assertIn("version=2.5.2", (self.target / "ModOrganizer.ini").read_text())

    def test_dry_run_writes_nothing(self):
        self.game.mkdir(parents=True)
        result = initialize_portable(self.target, self.archive, "Skyrim Special Edition", self.game, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertFalse((self.target / "ModOrganizer.ini").exists())
        self.assertFalse((self.target / "ModOrganizer.exe").exists())

    def test_refuses_non_empty_directory_and_non_mo2_archive(self):
        self.target.mkdir(parents=True)
        (self.target / "keep.txt").write_text("x")
        with self.assertRaises(Mo2Error):
            initialize_portable(self.target, self.archive, "Skyrim Special Edition", self.game)
        other = self.root / "other.zip"
        with zipfile.ZipFile(other, "w") as output:
            output.writestr("a.txt", b"1")
            output.writestr("b/c.txt", b"2")
        with self.assertRaises(Mo2Error):
            initialize_portable(self.root / "Fresh", other, "Skyrim Special Edition", self.game)
        self.assertFalse((self.root / "Fresh").exists())

    def test_archive_without_exe_inside_wrapper_aborts_before_writing(self):
        other = self.root / "wrapped.zip"
        with zipfile.ZipFile(other, "w") as output:
            output.writestr("Wrapper/readme.txt", b"1")
        with self.assertRaises(Mo2Error):
            initialize_portable(self.root / "Fresh", other, "Skyrim Special Edition", self.game)
        self.assertFalse((self.root / "Fresh" / "ModOrganizer.ini").exists())

    def test_missing_game_path_is_reported_not_fatal(self):
        result = initialize_portable(self.target, self.archive, "Skyrim Special Edition", self.game, my_games=self.my_games)
        self.assertFalse(result["game_path_exists"])
        self.assertIn("warning", result)

    def test_game_edition_detection_and_render(self):
        self.game.mkdir(parents=True)
        self.assertEqual(detect_game_edition(self.game), "Steam")
        (self.game / "Galaxy64.dll").write_bytes(b"")
        self.assertEqual(detect_game_edition(self.game), "GOG")
        (self.game / "Galaxy64.dll").unlink()
        (self.game / "EOSSDK-Win64-Shipping.dll").write_bytes(b"")
        self.assertEqual(detect_game_edition(self.game), "Epic Games")
        text = render_mo2_ini("Skyrim Special Edition", Path("C:/Modlists/X/Stock Game"), None, "Default", None)
        self.assertNotIn("game_edition", text)
        self.assertNotIn("version=", text)

    def test_cli_portable_json_and_validation(self):
        self.game.mkdir(parents=True)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--json", "instance", "init", str(self.target), "--portable", "--mo2-archive", str(self.archive), "--game-name", "Skyrim Special Edition", "--game-path", str(self.game), "--my-games", str(self.my_games), "--mo2-url", "https://example.test/Mod.Organizer-2.5.2.zip"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buffer.getvalue())["profile"], "Default")
        self.assertTrue((self.target / "portable.txt").is_file())
        self.assertEqual(main(["instance", "init", str(self.root / "X"), "--portable", "--game-name", "g", "--game-path", str(self.game)]), 2)
        self.assertEqual(main(["instance", "init", str(self.root / "Y"), "--mo2-archive", str(self.archive), "--game-name", "g", "--game-path", str(self.game)]), 2)

    def test_legacy_non_portable_init_still_works(self):
        self.assertEqual(main(["instance", "init", str(self.root / "Legacy"), "--game-name", "Skyrim Special Edition", "--game-path", str(self.game)]), 0)
        self.assertTrue((self.root / "Legacy" / "ModOrganizer.ini").is_file())


if __name__ == "__main__":
    unittest.main()
