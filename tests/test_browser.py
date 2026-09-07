import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path


from mo2cli.browser import (
    FAST_FORWARD_SCRIPT,
    _browser_profile_dir,
    _detect_browser_channel,
    resolve_nxm_url,
    batch_resolve_nxm_urls,
)
from mo2cli.workspace import Mo2Error


class BrowserTests(unittest.TestCase):
    def test_fast_forward_script_contains_override(self):
        self.assertIn("window.setTimeout", FAST_FORWARD_SCRIPT)
        self.assertIn("delay = 50", FAST_FORWARD_SCRIPT)

    def test_browser_profile_dir(self):
        profile_dir = _browser_profile_dir()
        self.assertIsInstance(profile_dir, Path)
        self.assertTrue(profile_dir.is_dir())

    def test_detect_browser_channel(self):
        channel = _detect_browser_channel()
        self.assertIn(channel, ["msedge", "chrome", None])

    def test_resolve_nxm_url_simulated(self):
        with patch.dict("sys.modules", {"playwright": MagicMock(), "playwright.sync_api": MagicMock()}):
            mock_playwright_ctx = MagicMock()
            mock_browser_ctx = MagicMock()
            mock_page = MagicMock()

            mock_playwright_module = MagicMock()
            mock_playwright_module.sync_playwright.return_value.__enter__.return_value = mock_playwright_ctx
            mock_playwright_ctx.chromium.launch_persistent_context.return_value = mock_browser_ctx
            mock_browser_ctx.new_page.return_value = mock_page

            test_nxm = "nxm://skyrimspecialedition/mods/100/files/200?key=abcdef123&expires=1800000000&user_id=12345"

            def simulate_listeners(event, callback):
                if event == "request":
                    req = MagicMock()
                    req.url = test_nxm
                    callback(req)

            mock_page.on.side_effect = simulate_listeners

            with patch("playwright.sync_api.sync_playwright", mock_playwright_module.sync_playwright):
                res = resolve_nxm_url("https://www.nexusmods.com/skyrimspecialedition/mods/100?tab=files&file_id=200", timeout=2.0)
                self.assertEqual(res, test_nxm)

    def test_missing_playwright_raises_mo2error(self):
        with patch.dict("sys.modules", {"playwright": None, "playwright.sync_api": None}):
            with self.assertRaises(Mo2Error) as ctx:
                resolve_nxm_url("https://www.nexusmods.com/skyrimspecialedition/mods/100?tab=files&file_id=200")
            self.assertIn("Playwright is required", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
