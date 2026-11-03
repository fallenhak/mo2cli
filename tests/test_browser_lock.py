import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mo2cli import browser
from mo2cli.workspace import Mo2Error


class BrowserProfileLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = patch("mo2cli.browser.Path.home", return_value=Path(self.tmp.name))
        self.home.start()
        self.lock = Path(self.tmp.name) / ".mo2cli" / "browser_profile.lock"

    def tearDown(self):
        self.home.stop()
        self.tmp.cleanup()

    def test_second_user_waits_for_first(self):
        order = []

        def first():
            with browser._profile_lock(poll=0.01):
                order.append("first-in")
                time.sleep(0.2)
                order.append("first-out")

        thread = threading.Thread(target=first)
        thread.start()
        time.sleep(0.05)
        with browser._profile_lock(poll=0.01):
            order.append("second-in")
        thread.join()
        self.assertEqual(order, ["first-in", "first-out", "second-in"])
        self.assertFalse(self.lock.exists())

    def test_stale_lock_taken_over_and_busy_lock_times_out(self):
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_text("99999999", encoding="utf-8")
        with patch("mo2cli.browser._pid_alive", return_value=False):
            with browser._profile_lock(poll=0.01):
                self.assertEqual(self.lock.read_text(encoding="utf-8"), str(os.getpid()))
        self.lock.write_text("12345", encoding="utf-8")
        with patch("mo2cli.browser._pid_alive", return_value=True):
            with self.assertRaises(Mo2Error):
                with browser._profile_lock(timeout=0.05, poll=0.01):
                    pass


if __name__ == "__main__":
    unittest.main()
