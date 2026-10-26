"""Read-only lookups of public mod list data: Nexus Collections and Load Order Library."""

from __future__ import annotations

from typing import Any

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
