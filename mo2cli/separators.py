from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

from .formats import ModList, write_text
from .journal import record
from .workspace import Instance, Mo2Error, _safe_name


def _display_name(name: str) -> str:
    for suffix in ("_separator", "separator"):
        if name.casefold().endswith(suffix):
            return name[:-len(suffix)]
    return name


def _separator_candidates(name: str) -> list[str]:
    raw = name.strip()
    display = _display_name(raw).rstrip()
    candidates = [raw]
    for suffix in ("_separator", "separator"):
        candidate = f"{display}{suffix}"
        if candidate.casefold() not in {item.casefold() for item in candidates}:
            candidates.append(candidate)
    return candidates


def _find_separator_entry(model: ModList, name: str):
    for candidate in _separator_candidates(name):
        entry = model.find(candidate)
        if entry is not None:
            return entry
    return None


def internal_name(name: str) -> str:
    display = _display_name(name.strip())
    _safe_name(display)
    return f"{display}_separator"


def _find_mod_path(instance: Instance, name: str) -> Path | None:
    return next((path for path in instance.mods_dir.iterdir() if path.name.casefold() == name.casefold()), None) if instance.mods_dir.exists() else None


def _find_separator_path(instance: Instance, name: str) -> Path | None:
    for candidate in _separator_candidates(name):
        path = _find_mod_path(instance, candidate)
        if path is not None:
            return path
    return None


def _profiles(instance: Instance, profile: str | None, all_profiles: bool) -> list[str]:
    return instance.list_profiles() if all_profiles else [instance.profile_name(profile)]


def _backups(instance: Instance, profiles: list[str]) -> list[dict[str, str]]:
    result = []
    for name in profiles:
        path = instance.profiles_dir / name / "modlist.txt"
        result.append({"path": str(path), "content": path.read_text(encoding="utf-8") if path.exists() else ""})
    return result


def _initialize_metadata(path: Path) -> None:
    write_text(path / "meta.ini", "modid=0\r\nversion=\r\nnewestVersion=\r\ncategory=0\r\ninstallationFile=\r\n")


def _place(model: ModList, name: str, before: str | None, after: str | None) -> None:
    if before and after:
        raise Mo2Error("Cannot specify --before and --after simultaneously.")
    if not (before or after):
        return
    target = before or after
    target_entry = model.find(target)
    if target_entry is None:
        target_entry = _find_separator_entry(model, target)
    if target_entry is None:
        raise Mo2Error(f"Target mod not found: {target}")
    target_index = model.entries.index(target_entry) + (1 if after else 0)
    model.move(name, min(target_index, len(model.entries) - 1))


def create(instance: Instance, profile: str | None, name: str, before: str | None = None, after: str | None = None, all_profiles: bool = False) -> dict[str, object]:
    internal = internal_name(name)
    existing = _find_separator_path(instance, name)
    if existing is not None:
        raise Mo2Error(f"Separator already exists: {_display_name(existing.name)}")
    profiles = _profiles(instance, profile, all_profiles)
    backups = _backups(instance, profiles)
    instance.mods_dir.mkdir(parents=True, exist_ok=True)
    path = instance.mods_dir / internal
    path.mkdir(parents=False)
    _initialize_metadata(path)
    try:
        for profile_name in profiles:
            profile_path = instance.profiles_dir / profile_name
            model = ModList.read(profile_path / "modlist.txt")
            if model.find(internal) is not None:
                raise Mo2Error(f"Separator already exists in profile: {_display_name(internal)}")
            model.add(internal, enabled=True)
            _place(model, internal, before, after)
            write_text(profile_path / "modlist.txt", model.render())
    except Exception:
        shutil.rmtree(path, ignore_errors=True)
        raise
    entry = record(instance, "separator_create", path=str(path), profiles=backups)
    return {"separator": _display_name(internal), "internal_name": internal, "path": str(path), "profiles": profiles, "journal_id": entry["id"]}


def ensure(instance: Instance, profile: str | None, name: str, before: str | None = None, after: str | None = None) -> dict[str, object]:
    internal = internal_name(name)
    path = _find_separator_path(instance, name)
    selected = instance.profile_name(profile)
    profile_path = instance.profiles_dir / selected
    model = ModList.read(profile_path / "modlist.txt")
    if path is not None and not path.is_dir():
        raise Mo2Error(f"Separator path is not a directory: {path}")
    existing_entry = _find_separator_entry(model, name)
    if path is not None and existing_entry is not None:
        return {"separator": _display_name(existing_entry.name), "internal_name": existing_entry.name, "path": str(path), "existing": True, "profiles": [selected]}
    if path is None:
        return create(instance, selected, name, before, after)
    if not (path / "meta.ini").exists():
        _initialize_metadata(path)
    backup = _backups(instance, [selected])
    actual_internal = path.name if path is not None else internal
    model.add(actual_internal, enabled=True)
    _place(model, actual_internal, before, after)
    write_text(profile_path / "modlist.txt", model.render())
    entry = record(instance, "separator_create", path=str(path), profiles=backup)
    return {"separator": _display_name(actual_internal), "internal_name": actual_internal, "path": str(path), "existing": True, "profiles": [selected], "journal_id": entry["id"]}


def list_separators(instance: Instance, profile: str | None = None) -> list[dict[str, object]]:
    _, model, _ = instance.profile_files(profile)
    result = []
    for index, entry in enumerate(model.entries):
        if entry.name.casefold().endswith("separator"):
            result.append({"index": index, "name": _display_name(entry.name), "internal_name": entry.name, "enabled": entry.enabled, "path": str(instance.mods_dir / entry.name)})
    return result


def remove(instance: Instance, profile: str | None, name: str, purge: bool = False, yes: bool = False) -> dict[str, object]:
    if not yes:
        raise Mo2Error("Separator removal requires --yes.")
    internal = internal_name(name)
    path = _find_separator_path(instance, name)
    if path is not None:
        internal = path.name
    if path is None or not path.is_dir():
        raise Mo2Error(f"Separator not found: {_display_name(internal)}")
    profiles = instance.list_profiles()
    backups = _backups(instance, profiles)
    trash = None
    if purge:
        shutil.rmtree(path)
    else:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        trash = instance.base / ".mo2cli-trash" / f"{path.name}-{stamp}"
        trash.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(trash))
    for profile_name in profiles:
        profile_path = instance.profiles_dir / profile_name
        model = ModList.read(profile_path / "modlist.txt")
        model.lines = [line for line in model.lines if not (hasattr(line, "name") and line.name.casefold() == internal.casefold())]
        write_text(profile_path / "modlist.txt", model.render())
    entry = record(instance, "separator_remove", reversible=not purge, path=str(instance.mods_dir / internal), trash=str(trash) if trash else None, profiles=backups)
    return {"removed": _display_name(internal), "internal_name": internal, "purged": purge, "trash": str(trash) if trash else None, "journal_id": entry["id"]}


def group(instance: Instance, profile: str | None, separator: str, names: list[str]) -> dict[str, object]:
    if not names:
        raise Mo2Error("At least one mod required for group.")
    internal = internal_name(separator)
    profile_name = instance.profile_name(profile)
    profile_path = instance.profiles_dir / profile_name
    modlist_path = profile_path / "modlist.txt"
    backup = _backups(instance, [profile_name])
    model = ModList.read(modlist_path)
    separator_entry = _find_separator_entry(model, separator)
    if separator_entry is None:
        raise Mo2Error(f"Separator not found: {_display_name(internal)}")
    for name in names:
        if model.find(name) is None:
            raise Mo2Error(f"Mod not found in profile: {name}")
    # MO2 writes modlist.txt in reverse of the ascending UI priority order.
    # To display names under a separator, they must therefore be immediately
    # before that separator in the file. Reverse the requested UI order while
    # placing multiple mods so the visible order remains stable.
    for name in reversed(names):
        separator_index = model.entries.index(separator_entry)
        current = next(index for index, entry in enumerate(model.entries) if entry.name.casefold() == name.casefold())
        target = separator_index - 1 if current < separator_index else separator_index
        model.move(name, target)
    write_text(modlist_path, model.render())
    entry = record(instance, "separator_group", separator=internal, profiles=backup, names=names)
    return {"separator": _display_name(internal), "profile": profile_name, "mods": names, "journal_id": entry["id"]}
