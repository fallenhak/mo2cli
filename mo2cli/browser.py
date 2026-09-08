from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from .workspace import Mo2Error


FAST_FORWARD_SCRIPT = """
(() => {
    try {
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    } catch (e) {}

    const origSetTimeout = window.setTimeout;
    window.setTimeout = function(callback, delay, ...args) {
        try {
            const host = window.location.hostname || '';
            const title = document.title || '';
            if (host.includes('cloudflare') || title.includes('Bir dakika') || title.includes('Just a moment')) {
                return origSetTimeout(callback, delay, ...args);
            }
            if (host.includes('nexusmods.com') && typeof delay === 'number' && delay >= 1000 && delay <= 10000) {
                delay = 50;
            }
        } catch (e) {}
        return origSetTimeout(callback, delay, ...args);
    };
})();
"""


def _browser_profile_dir() -> Path:
    profile_dir = Path.home() / ".mo2cli" / "browser_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir


def _detect_browser_channel() -> str | None:
    if os.name == "nt":
        edge_paths = [
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        ]
        if any(p.is_file() for p in edge_paths) or shutil.which("msedge"):
            return "msedge"
        chrome_paths = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        ]
        if any(p.is_file() for p in chrome_paths) or shutil.which("chrome"):
            return "chrome"
    return None


def interactive_login() -> None:
    """Launch a visible browser to allow the user to log in to Nexus Mods once."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise Mo2Error("Playwright is required for browser automation. Run: pip install playwright")

    profile_dir = _browser_profile_dir()
    channel = _detect_browser_channel()
    print("Opening browser for Nexus Mods login...")
    print("Please log in to your account. Once logged in, you can close the browser or wait.")

    with sync_playwright() as p:
        kwargs: dict[str, Any] = {
            "user_data_dir": str(profile_dir),
            "headless": False,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if channel:
            kwargs["channel"] = channel

        context = p.chromium.launch_persistent_context(**kwargs)
        page = context.new_page()
        page.goto("https://users.nexusmods.com/auth/sign_in", wait_until="domcontentloaded")

        start_time = time.time()
        logged_in = False
        while time.time() - start_time < 300:
            try:
                cookies = context.cookies(["https://www.nexusmods.com", "https://nexusmods.com", "https://users.nexusmods.com"])
                cookie_names = {c["name"].lower() for c in cookies}
                url = page.url.lower()
                if "sid_token" in cookie_names or "remember_user_token" in cookie_names or "nexusmods_user" in cookie_names or ("users.nexusmods.com" in url and "sign_in" not in url and "auth" not in url):
                    logged_in = True
                    break
            except Exception:
                pass
            time.sleep(1.0)

        context.close()
        if logged_in:
            print("Successfully logged in! Session saved for automated downloads.")
        else:
            print("Login timed out or window closed. Check your browser session.")


def resolve_nxm_url(page_url: str, timeout: float = 35.0) -> str:
    """Navigate to a Nexus Mods file page, trigger Slow Download, and capture the nxm:// URL."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise Mo2Error("Playwright is required for browser automation. Run: pip install playwright")

    profile_dir = _browser_profile_dir()
    channel = _detect_browser_channel()

    target_url = page_url
    if "tab=files" not in target_url and "files/" in target_url:
        parts = target_url.split("files/", 1)
        file_id = parts[1].split("?")[0].split("/")[0]
        target_url = f"{parts[0]}?tab=files&file_id={file_id}"
    if "nmm=1" not in target_url:
        delimiter = "&" if "?" in target_url else "?"
        target_url = f"{target_url}{delimiter}nmm=1"

    captured_url: str | None = None

    with sync_playwright() as p:
        kwargs: dict[str, Any] = {
            "user_data_dir": str(profile_dir),
            "headless": False,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        }
        if channel:
            kwargs["channel"] = channel

        context = p.chromium.launch_persistent_context(**kwargs)
        context.add_init_script(FAST_FORWARD_SCRIPT)

        page = context.new_page()

        def on_request(request):
            nonlocal captured_url
            if request.url.startswith("nxm://"):
                captured_url = request.url

        def on_response(response):
            nonlocal captured_url
            if "GenerateDownloadUrl" in response.url or "download_link" in response.url:
                try:
                    data = response.json()
                    if isinstance(data, dict):
                        uri = data.get("URI") or data.get("uri") or data.get("url")
                        if uri and (str(uri).startswith("nxm://") or str(uri).startswith("http://") or str(uri).startswith("https://")):
                            captured_url = str(uri)
                    elif isinstance(data, list):
                        for item in data:
                            if isinstance(item, dict):
                                uri = item.get("URI") or item.get("uri") or item.get("url")
                                if uri and (str(uri).startswith("nxm://") or str(uri).startswith("http://") or str(uri).startswith("https://")):
                                    captured_url = str(uri)
                                    break
                except Exception:
                    pass

        def on_download(download):
            nonlocal captured_url
            captured_url = download.url
            try:
                download.cancel()
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("download", on_download)

        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=timeout * 1000)
        except Exception:
            pass

        start_time = time.time()
        while time.time() - start_time < timeout:
            if captured_url:
                break

            try:
                page.evaluate("""() => {
                    const cb = document.getElementById('CybotCookiebotDialog');
                    if (cb) cb.remove();
                    const under = document.getElementById('CybotCookiebotDialogUnderlay');
                    if (under) under.remove();
                }""")
            except Exception:
                pass

            for selector in [
                ".nxm-button-secondary-filled-weak",
                "button.nxm-button-secondary-filled-weak",
                "#slowDownloadButton",
                "button#slowDownloadButton",
                "button.btn-slow",
                "button:has-text('Slow Download')",
                "a:has-text('Slow Download')",
                "button:has-text('Slow download')",
                "a:has-text('Slow download')",
                "a[data-download-type='slow']",
            ]:
                try:
                    element = page.locator(selector).first
                    if element.is_visible(timeout=500):
                        element.click(timeout=1000, force=True)
                        break
                except Exception:
                    continue

            time.sleep(0.5)

        context.close()

    if not captured_url:
        raise Mo2Error(
            f"Failed to capture NXM download link automatically for: {page_url}. "
            "Make sure you are logged in via 'mo2 nexus login' or check your internet connection."
        )

    return captured_url


def batch_resolve_nxm_urls(urls: list[str], concurrency: int = 3, timeout: float = 40.0) -> dict[str, str]:
    """Resolve multiple Nexus mod file URLs in parallel browser tabs."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise Mo2Error("Playwright is required for browser automation. Run: pip install playwright")

    if not urls:
        return {}

    profile_dir = _browser_profile_dir()
    channel = _detect_browser_channel()
    results: dict[str, str] = {}

    with sync_playwright() as p:
        kwargs: dict[str, Any] = {
            "user_data_dir": str(profile_dir),
            "headless": False,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        }
        if channel:
            kwargs["channel"] = channel

        context = p.chromium.launch_persistent_context(**kwargs)
        context.add_init_script(FAST_FORWARD_SCRIPT)

        for i in range(0, len(urls), concurrency):
            batch = urls[i : i + concurrency]
            tabs_data: list[dict[str, Any]] = []

            for raw_url in batch:
                target_url = raw_url
                if "tab=files" not in target_url and "files/" in target_url:
                    parts = target_url.split("files/", 1)
                    file_id = parts[1].split("?")[0].split("/")[0]
                    target_url = f"{parts[0]}?tab=files&file_id={file_id}"
                if "nmm=1" not in target_url:
                    delimiter = "&" if "?" in target_url else "?"
                    target_url = f"{target_url}{delimiter}nmm=1"

                page = context.new_page()
                info = {"url": raw_url, "target_url": target_url, "page": page, "captured": None}

                def make_request_handler(item):
                    def handler(request):
                        if request.url.startswith("nxm://"):
                            item["captured"] = request.url
                    return handler

                def make_response_handler(item):
                    def handler(response):
                        if "GenerateDownloadUrl" in response.url or "download_link" in response.url:
                            try:
                                data = response.json()
                                if isinstance(data, dict):
                                    uri = data.get("URI") or data.get("uri") or data.get("url")
                                    if uri and (str(uri).startswith("nxm://") or str(uri).startswith("http://") or str(uri).startswith("https://")):
                                        item["captured"] = str(uri)
                            except Exception:
                                pass
                    return handler

                def make_download_handler(item):
                    def handler(download):
                        item["captured"] = download.url
                        try:
                            download.cancel()
                        except Exception:
                            pass
                    return handler

                page.on("request", make_request_handler(info))
                page.on("response", make_response_handler(info))
                page.on("download", make_download_handler(info))

                try:
                    page.goto(target_url, wait_until="domcontentloaded", timeout=timeout * 1000)
                except Exception:
                    pass

                tabs_data.append(info)

            start_time = time.time()
            while time.time() - start_time < timeout:
                if all(item["captured"] for item in tabs_data):
                    break

                for item in tabs_data:
                    if item["captured"]:
                        continue
                    page = item["page"]
                    try:
                        page.evaluate("""() => {
                            const cb = document.getElementById('CybotCookiebotDialog');
                            if (cb) cb.remove();
                            const under = document.getElementById('CybotCookiebotDialogUnderlay');
                            if (under) under.remove();
                        }""")
                    except Exception:
                        pass
                    for selector in [
                        ".nxm-button-secondary-filled-weak",
                        "button.nxm-button-secondary-filled-weak",
                        "#slowDownloadButton",
                        "button#slowDownloadButton",
                        "button.btn-slow",
                        "button:has-text('Slow Download')",
                        "a:has-text('Slow Download')",
                        "button:has-text('Slow download')",
                        "a:has-text('Slow download')",
                        "a[data-download-type='slow']",
                    ]:
                        try:
                            element = page.locator(selector).first
                            if element.is_visible(timeout=300):
                                element.click(timeout=500, force=True)
                                break
                        except Exception:
                            continue

                time.sleep(0.5)

            for item in tabs_data:
                if item["captured"]:
                    results[item["url"]] = str(item["captured"])
                item["page"].close()

        context.close()

    return results
