import unittest

from mo2cli import qsettings, separators
from mo2cli.formats import ModList
from mo2cli.journal import undo_last
from mo2cli.workspace import Instance, Mo2Error


class SeparatorApplyTests(unittest.TestCase):
    def setUp(self):
        from test_workspace import WorkspaceTests

        self.fixture = WorkspaceTests()
        self.fixture.setUp()
        self.root = self.fixture.make_instance()
        self.instance = Instance.open(self.root)
        self.list = self.root / "separators.txt"

    def tearDown(self):
        self.fixture.tearDown()

    def order(self):
        # visible order, top to bottom
        names = [e.name for e in ModList.read(self.root / "profiles" / "Default" / "modlist.txt").entries]
        return [n for n in reversed(names) if n.endswith("_separator")]

    def test_qcolor_variant_matches_qt_layout(self):
        value = qsettings.encode_qcolor("#8caae5")
        self.assertEqual(value, "@Variant(" + chr(92) + "0" + chr(92) + "0" + chr(92) + "0C" + chr(92) + "0" + chr(92) + "x1" + (chr(92) + "xff") * 2
                         + (chr(92) + "x8c") * 2 + (chr(92) + "xaa") * 2 + (chr(92) + "xe5") * 2 + chr(92) + "0" + chr(92) + "0)")
        self.assertEqual(qsettings.decode_qcolor(value), "#8caae5")
        self.assertEqual(qsettings.decode_qcolor(qsettings.encode_qcolor("#41ff00")), "#41ff00")
        self.assertIsNone(qsettings.decode_qcolor("@ByteArray(x)"))
        with self.assertRaises(ValueError):
            qsettings.encode_qcolor("red")

    def test_create_with_color_writes_meta(self):
        separators.create(self.instance, None, "Armor", color="#102030")
        self.assertEqual(separators.get_color(self.root / "mods" / "Armor_separator"), "#102030")
        with self.assertRaises(ValueError):
            separators.create(self.instance, None, "Bad", color="#12")
        self.assertFalse((self.root / "mods" / "Bad_separator").exists())

    def test_set_color_replaces_and_respects_general_header(self):
        path = self.root / "mods" / "S_separator"
        path.mkdir()
        (path / "meta.ini").write_text("[General]\r\nmodid=0\r\n", newline="")
        self.assertTrue(separators.set_color(path, "#ffffff"))
        self.assertFalse(separators.set_color(path, "#ffffff"))
        self.assertTrue(separators.set_color(path, "#000001"))
        text = (path / "meta.ini").read_bytes().decode()
        self.assertEqual(text.count("color="), 1)
        self.assertTrue(text.startswith("[General]\r\ncolor="))
        self.assertEqual(separators.get_color(path), "#000001")

    def test_apply_creates_orders_and_colors(self):
        self.list.write_text("; header\nGameplay | #FF0000\nVisuals\n\nTools | #00ff00\n", encoding="utf-8")
        result = separators.apply_list(self.instance, None, self.list)
        self.assertEqual(result["created"], ["Gameplay", "Visuals", "Tools"])
        self.assertEqual(self.order(), ["Gameplay_separator", "Visuals_separator", "Tools_separator"])
        self.assertEqual(separators.get_color(self.root / "mods" / "Gameplay_separator"), "#ff0000")
        self.assertIsNone(separators.get_color(self.root / "mods" / "Visuals_separator"))

    def test_apply_is_idempotent(self):
        self.list.write_text("A | #111111\nB\nC | #333333\n", encoding="utf-8")
        separators.apply_list(self.instance, None, self.list)
        modlist = (self.root / "profiles" / "Default" / "modlist.txt").read_bytes()
        journal_size = len((self.root / ".mo2cli" / "journal.jsonl").read_bytes()) if (self.root / ".mo2cli" / "journal.jsonl").exists() else 0
        again = separators.apply_list(self.instance, None, self.list)
        self.assertFalse(again["changed"])
        self.assertEqual((self.root / "profiles" / "Default" / "modlist.txt").read_bytes(), modlist)
        if journal_size:
            self.assertEqual(len((self.root / ".mo2cli" / "journal.jsonl").read_bytes()), journal_size)

    def test_apply_reorders_existing_without_touching_mods(self):
        separators.create(self.instance, None, "B")
        separators.create(self.instance, None, "A")  # visible order now: A, B... file order B, A? check below
        self.list.write_text("B\nA\n", encoding="utf-8")
        separators.apply_list(self.instance, None, self.list)
        self.assertEqual(self.order(), ["B_separator", "A_separator"])
        names = [e.name for e in ModList.read(self.root / "profiles" / "Default" / "modlist.txt").entries]
        self.assertIn("Patch Mod", names)
        self.assertIn("Base Mod", names)

    def test_dry_run_changes_nothing(self):
        self.list.write_text("A | #111111\nB\n", encoding="utf-8")
        before = (self.root / "profiles" / "Default" / "modlist.txt").read_bytes()
        result = separators.apply_list(self.instance, None, self.list, dry_run=True)
        self.assertEqual(result["created"], ["A", "B"])
        self.assertEqual((self.root / "profiles" / "Default" / "modlist.txt").read_bytes(), before)
        self.assertFalse((self.root / "mods" / "A_separator").exists())

    def test_recolor_existing_and_undo(self):
        separators.create(self.instance, None, "A", color="#111111")
        meta_before = (self.root / "mods" / "A_separator" / "meta.ini").read_bytes()
        self.list.write_text("A | #222222\nNew\n", encoding="utf-8")
        result = separators.apply_list(self.instance, None, self.list)
        self.assertEqual(result["recolored"], ["A"])
        self.assertEqual(result["created"], ["New"])
        undo_last(self.instance)
        self.assertEqual((self.root / "mods" / "A_separator" / "meta.ini").read_bytes(), meta_before)
        self.assertFalse((self.root / "mods" / "New_separator").exists())
        self.assertEqual(self.order(), ["A_separator"])

    def test_list_validation(self):
        for text in ("", "A | red\n", "A |\n", "A\na\n", "a/b\n"):
            self.list.write_text(text, encoding="utf-8")
            with self.assertRaises(Mo2Error, msg=text):
                separators.apply_list(self.instance, None, self.list)
        with self.assertRaises(Mo2Error):
            separators.apply_list(self.instance, None, self.root / "missing.txt")


if __name__ == "__main__":
    unittest.main()
