"""Resolve ``github:owner/repo@latest|TAG`` references to a release asset download."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import __version__
from .downloads import fetch
from .workspace import Instance, Mo2Error

API_ROOT = "https://api.github.com"
PREFIX = "github:"
_REF = re.compile(r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)(?:@(?P<tag>.+))?$")


def is_github_ref(value: str) -> bool:
    return str(value).casefold().startswith(PREFIX)


def parse_ref(value: str) -> tuple[str, str, str]:
    match = _REF.match(str(value)[len(PREFIX):].strip())
    if not str(value).casefold().startswith(PREFIX) or not match:
        raise Mo2Error("GitHub reference must look like github:owner/repo@latest or github:owner/repo@TAG.")
    return match["owner"], match["repo"], match["tag"] or "latest"


def _api(path: str) -> Any:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": f"mo2cli/{__version__}", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{API_ROOT}{path}", headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise Mo2Error(f"GitHub API HTTP {error.code} for {path}: {detail[:300]}") from error
    except (urllib.error.URLError, json.JSONDecodeError) as error:
        raise Mo2Error(f"GitHub API request failed: {error}") from error


def resolve_asset(reference: str, asset_glob: str | None = None) -> dict[str, object]:
    """Pick one release asset. Without ``asset_glob`` the release must have exactly one asset."""
    owner, repo, tag = parse_ref(reference)
    base = f"/repos/{owner}/{repo}/releases"
    release = _api(f"{base}/latest" if tag.casefold() == "latest" else f"{base}/tags/{urllib.parse.quote(tag, safe='')}")
    assets = [item for item in (release.get("assets") or []) if isinstance(item, dict) and item.get("browser_download_url")]
    names = [str(item.get("name")) for item in assets]
    if asset_glob:
        picked = [item for item in assets if fnmatch.fnmatchcase(str(item.get("name", "")).casefold(), asset_glob.casefold())]
    else:
        picked = assets
    if not picked:
        raise Mo2Error(f"No release asset matches {asset_glob or '(any)'} in {owner}/{repo}@{release.get('tag_name')}. Assets: {', '.join(names) or 'none'}")
    if len(picked) > 1:
        raise Mo2Error(f"{len(picked)} assets match {asset_glob or '(any)'}; narrow --asset. Candidates: {', '.join(str(i.get('name')) for i in picked)}")
    asset = picked[0]
    return {
        "owner": owner,
        "repo": repo,
        "tag": release.get("tag_name"),
        "asset": asset.get("name"),
        "size": asset.get("size"),
        "url": asset["browser_download_url"],
    }


def fetch_asset(instance: Instance, reference: str, asset_glob: str | None = None, output: str | None = None, replace: bool = False, expected_sha256: str | None = None, headers: dict[str, str] | None = None) -> dict[str, object]:
    """Download the asset; ``.meta`` gets ``directURL`` = the asset's ``browser_download_url``."""
    resolved = resolve_asset(reference, asset_glob)
    result = fetch(instance, str(resolved["url"]), output or str(resolved["asset"]), replace, headers=headers, expected_sha256=expected_sha256)
    result["github"] = {key: resolved[key] for key in ("owner", "repo", "tag", "asset")}
    return result
