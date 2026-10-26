"""Read-only lookups of public mod list data: Nexus Collections and Load Order Library."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__
from .nexus import NexusClient
from .workspace import Mo2Error

COLLECTION_QUERY = (
    "query Collection($slug: String!, $revision: Int) { collectionRevision(slug: $slug, revision: $revision, viewAdultContent: true) "
    "{ revisionNumber modCount collection { name slug } "
    "modFiles { optional file { fileId name version mod { modId name game { domainName } } } } } }"
)


def collection(slug: str, revision: int | None = None, api_key: str | None = None, client: NexusClient | None = None) -> dict[str, Any]:
    """Mods of a Nexus Collection revision (GraphQL v2 ``collectionRevision``); latest revision when ``revision`` is None."""
    nexus = client or NexusClient(api_key)
    data = nexus._graphql(COLLECTION_QUERY, {"slug": slug, "revision": revision})
    node = (data or {}).get("collectionRevision") if isinstance(data, dict) else None
    if not isinstance(node, dict):
        raise Mo2Error(f"Collection not found or no access: {slug}" + (f" revision {revision}" if revision else ""))
    mods = []
    for item in node.get("modFiles") or []:
        file = item.get("file") if isinstance(item, dict) else None
        if not isinstance(file, dict):
            continue
        mod = file.get("mod") or {}
        game = mod.get("game") or {}
        mods.append({
            "name": mod.get("name"),
            "modId": mod.get("modId"),
            "fileId": file.get("fileId"),
            "fileName": file.get("name"),
            "version": file.get("version"),
            "optional": bool(item.get("optional")),
            "game": game.get("domainName"),
        })
    info = node.get("collection") or {}
    return {
        "slug": info.get("slug") or slug,
        "name": info.get("name"),
        "revision": node.get("revisionNumber"),
        "modCount": node.get("modCount"),
        "listed": len(mods),
        "optional": sum(1 for item in mods if item["optional"]),
        "mods": mods,
    }


LOL_API = "https://api.loadorderlibrary.com/v1/lists/"


def _http_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": f"mo2cli/{__version__}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise Mo2Error(f"Load Order Library list not found: {url.rsplit('/', 1)[-1]}") from error
        raise Mo2Error(f"Load Order Library HTTP {error.code}") from error
    except (urllib.error.URLError, json.JSONDecodeError) as error:
        raise Mo2Error(f"Load Order Library request failed: {error}") from error


def lol(slug: str, save_to: str | None = None, include_content: bool = False) -> dict[str, Any]:
    """A Load Order Library list and its files (``modlist.txt``, ``plugins.txt``...); optionally saved to a folder."""
    data = _http_json(LOL_API + urllib.parse.quote(slug, safe="-_."))
    node = data.get("data") if isinstance(data, dict) else None
    if not isinstance(node, dict):
        raise Mo2Error("Load Order Library returned unexpected data.")
    files = []
    for item in node.get("files") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("clean_name") or item.get("name") or "")
        content = item.get("content")
        lines = [str(line) for line in content] if isinstance(content, list) else str(content or "").splitlines()
        entry: dict[str, Any] = {"name": name, "size": item.get("size_in_bytes"), "lines": len(lines)}
        if include_content:
            entry["content"] = lines
        files.append(entry)
        if save_to:
            target = Path(save_to).expanduser()
            target.mkdir(parents=True, exist_ok=True)
            safe = Path(name).name
            if not safe:
                continue
            (target / safe).write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")
            entry["saved"] = str(target / safe)
    return {
        "slug": node.get("slug") or slug,
        "name": node.get("name"),
        "version": node.get("version"),
        "author": (node.get("author") or {}).get("name"),
        "game": (node.get("game") or {}).get("name"),
        "website": node.get("website"),
        "readme": node.get("readme"),
        "updated": node.get("updated"),
        "files": files,
    }
