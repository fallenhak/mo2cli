import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from mo2cli import installer
from mo2cli.cli import main


class NonInteractiveTests(unittest.TestCase):
    def test_env_variable_disables_prompts_even_with_a_tty(self):
        with patch.dict(os.environ, {"MO2CLI_NONINTERACTIVE": "1"}), patch("sys.stdin.isatty", return_value=True):
            self.assertFalse(installer.is_interactive())

    def test_tty_is_interactive_by_default(self):
        env = {k: v for k, v in os.environ.items() if k != "MO2CLI_NONINTERACTIVE"}
        with patch.dict(os.environ, env, clear=True), patch("sys.stdin.isatty", return_value=True):
            self.assertTrue(installer.is_interactive())
        with patch.dict(os.environ, env, clear=True), patch("sys.stdin.isatty", return_value=False):
            self.assertFalse(installer.is_interactive())

    def test_flag_sets_environment_and_works_on_any_command(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MO2CLI_NONINTERACTIVE", None)
            with redirect_stdout(io.StringIO()), patch("sys.stdin.isatty", return_value=True):
                main(["--non-interactive", "--instance", "nowhere", "mo2plugin", "list"])
                self.assertEqual(os.environ.get("MO2CLI_NONINTERACTIVE"), "1")
                self.assertFalse(installer.is_interactive())
            os.environ.pop("MO2CLI_NONINTERACTIVE", None)


if __name__ == "__main__":
    unittest.main()
