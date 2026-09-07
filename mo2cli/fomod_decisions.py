from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from .archives import sha256
from .workspace import Instance


SCHEMA_VERSION = 1


def decision_path(instance: Instance, profile: str | None = None) -> Path:
    profile_path = instance.profile_path(profile)
    return profile_path / ".mo2cli" / "fomod-decisions.json"


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": SCHEMA_VERSION, "decisions": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {"version": SCHEMA_VERSION, "decisions": []}
    if not isinstance(value, dict) or not isinstance(value.get("decisions", []), list):
        return {"version": SCHEMA_VERSION, "decisions": []}
    return value


def context_snapshot(instance: Instance, profile: str | None = None) -> dict[str, Any]:
    _, mods, plugins = instance.profile_files(profile)
    return {
        "mods": [
            entry.name
            for entry in mods.entries
            if entry.enabled and not entry.foreign and not entry.name.casefold().endswith("_separator")
        ],
        "plugins": [entry.name for entry in plugins.entries if entry.enabled],
    }


def archive_fingerprint(archive: Path) -> str:
    return sha256(archive)


def find(
    instance: Instance,
    profile: str | None,
    archive: Path,
    module_name: str,
    config_hash: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    data = _read(decision_path(instance, profile))
    decisions = [item for item in data["decisions"] if isinstance(item, dict)]
    archive_hash = archive_fingerprint(archive)
    module_key = module_name.casefold()
    exact: list[dict[str, Any]] = []
    compatible: list[dict[str, Any]] = []
    for item in decisions:
        if item.get("module_name", "").casefold() != module_key:
            continue
        if item.get("archive_sha256") == archive_hash and item.get("config_sha256") == config_hash:
            exact.append(item)
        else:
            compatible.append(item)
    newest = lambda item: str(item.get("updated_at", item.get("created_at", "")))
    exact.sort(key=newest, reverse=True)
    compatible.sort(key=newest, reverse=True)
    return (exact[0] if exact else None, compatible[0] if compatible else None)


def save(instance: Instance, profile: str | None, decision: dict[str, Any]) -> Path:
    path = decision_path(instance, profile)
    data = _read(path)
    data["version"] = SCHEMA_VERSION
    data.setdefault("decisions", []).append({**decision, "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def list_decisions(instance: Instance, profile: str | None = None) -> list[dict[str, Any]]:
    data = _read(decision_path(instance, profile))
    return [item for item in data["decisions"] if isinstance(item, dict)]
