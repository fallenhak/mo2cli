"""File level conflict reports across the enabled mods of a profile.

Order of precedence for one virtual path: loose files by modlist priority
(``overwrite`` first), then, with ``include_bsa``, files inside archives that
belong to enabled plugins (the later plugin in the load order wins). Loose
files always beat archived ones, like in the game. Archives that ship with the
game itself are not part of the mod tree and are not considered.
"""

from __future__ import annotations

from pathlib import Path

from . import bsa
from .plugins import ordered_entries
from .workspace import Instance


def _normalise_extensions(value: str | list[str] | None) -> set[str] | None:
    if not value:
        return None
    items = value.split(",") if isinstance(value, str) else value
    result = {("." + item.strip().lstrip(".")).casefold() for item in items if item.strip()}
    return result or None


def _extension(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return "." + name.rsplit(".", 1)[-1] if "." in name else ""


def _bsa_providers(instance: Instance, profile: str | None, files: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    """Map archive internal path -> providers, strongest first."""
    winners = {str(item["path"]): item["winner"] for item in files if "/" not in str(item["path"])}
    plugins = [entry for entry in ordered_entries(instance, profile) if entry.enabled]
    found: dict[str, list[tuple[int, dict[str, object]]]] = {}
    for position, plugin in enumerate(plugins):
        if plugin.name.casefold() not in winners:
            continue
        stem = plugin.name.rsplit(".", 1)[0].casefold()
        for name, winner in winners.items():
            if not name.endswith(".bsa"):
                continue
            archive_stem = name[:-4]
            if archive_stem != stem and not archive_stem.startswith(stem + " - "):
                continue
            for inner in bsa.list_files(Path(str(winner["path"]))):
                found.setdefault(inner, []).append((position, {"mod": str(winner["mod"]), "kind": "bsa", "archive": str(winner["relative"]), "plugin": plugin.name}))
    return {inner: [item for _, item in sorted(entries, key=lambda pair: -pair[0])] for inner, entries in found.items()}


def _record(path: str, providers: list[dict[str, object]], wanted_mod: str | None, out: list[dict[str, object]], overwrites: dict[str, int], overwritten: dict[str, int]) -> None:
    if len({str(item["mod"]).casefold() for item in providers}) < 2:
        return
    winner = providers[0]
    losers: list[dict[str, object]] = []
    for item in providers[1:]:
        if item not in losers:
            losers.append(item)
    involved = {str(winner["mod"]).casefold()} | {str(item["mod"]).casefold() for item in losers}
    if wanted_mod and wanted_mod not in involved:
        return
    out.append({"path": path, "winner": winner, "losers": losers})
    overwrites[str(winner["mod"])] = overwrites.get(str(winner["mod"]), 0) + 1
    for name in {str(item["mod"]) for item in losers if str(item["mod"]).casefold() != str(winner["mod"]).casefold()}:
        overwritten[name] = overwritten.get(name, 0) + 1


def report(instance: Instance, profile: str | None = None, mod: str | None = None, extensions: str | list[str] | None = None, include_bsa: bool = False) -> dict[str, object]:
    exts = _normalise_extensions(extensions)
    wanted_mod = mod.casefold() if mod else None
    files = instance.virtual_files(profile)
    archived = _bsa_providers(instance, profile, files) if include_bsa else {}
    conflicts: list[dict[str, object]] = []
    overwrites: dict[str, int] = {}
    overwritten: dict[str, int] = {}
    loose = {str(entry["path"]): [{"mod": str(item["mod"]), "kind": "loose"} for item in entry["providers"]] for entry in files}
    for path in sorted(set(loose) | set(archived)):
        if exts and _extension(path) not in exts:
            continue
        _record(path, loose.get(path, []) + archived.get(path, []), wanted_mod, conflicts, overwrites, overwritten)
    names = sorted(set(overwrites) | set(overwritten), key=str.casefold)
    summary = [{"mod": name, "overwrites": overwrites.get(name, 0), "overwritten_by": overwritten.get(name, 0)} for name in names]
    if wanted_mod:
        summary = [item for item in summary if str(item["mod"]).casefold() == wanted_mod]
    return {"profile": instance.profile_name(profile), "include_bsa": include_bsa, "count": len(conflicts), "conflicts": conflicts, "mods": summary}


def redundant(instance: Instance, profile: str | None = None) -> dict[str, object]:
    """Enabled mods with no files at all, and mods whose every file is overwritten by a higher priority mod."""
    _, modlist, _ = instance.profile_files(profile)
    totals: dict[str, int] = {}
    wins: dict[str, int] = {}
    beaten_by: dict[str, set[str]] = {}
    for entry in instance.virtual_files(profile):
        winner = str(entry["providers"][0]["mod"])
        wins[winner] = wins.get(winner, 0) + 1
        for item in entry["providers"]:
            name = str(item["mod"])
            totals[name] = totals.get(name, 0) + 1
            if name != winner:
                beaten_by.setdefault(name, set()).add(winner)
    empty: list[dict[str, object]] = []
    fully_overwritten: list[dict[str, object]] = []
    for entry in modlist.entries:
        if not entry.enabled or entry.foreign or entry.name.casefold().endswith("_separator"):
            continue
        directory = instance._listed_mod_dir(entry.name)
        if directory is None or not directory.is_dir():
            continue
        total = totals.get(entry.name, 0)
        if total == 0:
            empty.append({"mod": entry.name})
        elif wins.get(entry.name, 0) == 0:
            fully_overwritten.append({"mod": entry.name, "files": total, "overwritten_by": sorted(beaten_by.get(entry.name, ()), key=str.casefold)})
    return {"profile": instance.profile_name(profile), "empty": empty, "fully_overwritten": fully_overwritten}
