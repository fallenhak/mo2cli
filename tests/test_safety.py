import json
import os
import unittest
from unittest.mock import patch

from mo2cli.safety import ensure_mo2_closed, mutation_guard
from mo2cli.workspace import Instance, Mo2Error


class SafetyTests(unittest.TestCase):
    def setUp(self):
        from test_workspace import WorkspaceTests

        self.fixture = WorkspaceTests()
        self.fixture.setUp()
        self.root = self.fixture.make_instance()
        self.instance = Instance.open(self.root)

    def tearDown(self):
        self.fixture.tearDown()

    def test_running_mo2_blocks_mutation(self):
        with patch("mo2cli.safety._running_process_names", return_value={"modorganizer.exe"}):
            with self.assertRaises(Mo2Error):
                ensure_mo2_closed()

    def test_mutation_guard_blocks_concurrent_writer_and_cleans_up(self):
        with patch("mo2cli.safety._running_process_names", return_value=set()):
            with mutation_guard(self.instance):
                lock_path = self.instance.base / ".mo2cli.lock"
                self.assertEqual(json.loads(lock_path.read_text(encoding="utf-8"))["pid"], os.getpid())
                with self.assertRaises(Mo2Error):
                    with mutation_guard(self.instance):
                        pass
            self.assertFalse((self.instance.base / ".mo2cli.lock").exists())

    def test_mutation_guard_recovers_stale_lock(self):
        lock_path = self.instance.base / ".mo2cli.lock"
        lock_path.write_text(json.dumps({"pid": 99999999, "token": "stale"}), encoding="utf-8")
        with patch("mo2cli.safety._running_process_names", return_value=set()), patch("mo2cli.safety._pid_alive", return_value=False):
            with mutation_guard(self.instance):
                self.assertTrue(lock_path.is_file())
        self.assertFalse(lock_path.exists())
