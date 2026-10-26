from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

from . import qsettings
from .formats import ModList, read_text_exact, write_text
from .journal import record
from .workspace import Instance, Mo2Error, _safe_name


def _display_name(name: str) -> str:
    for suffix in ("_separator", "separator"):
        if name.casefold().endswith(suffix):
            return name[:-len(suffix)]
    return name


def _separator_candidates(name: str) -> list[str]:
    exact = name
    raw = name.strip()
    candidates: list[str] = []

    def add(candidate: str) -> None:
        if candidate.casefold() not in {item.casefold() for item in candidates}:
            candidates.append(candidate)

    # Preserve the exact input first. Some existing MO2 separators contain a
    # significant space immediately before the internal ``_separator`` suffix,
    # and ``list`` exposes that same space in their display name.
    add(exact)
    add(raw)
    for display in (_display_name(exact), _display_name(raw).rstrip()):
        for suffix in ("_separator", "separator"):
            add(f"{display}{suffix}")
            # MO2 lists commonly use a display name with a trailing space,
            # which becomes a space immediately before the internal suffix.
            # Treat the visually identical trimmed display name as the same
            # separator instead of creating a duplicate at the end of a list.
            add(f"{display.rstrip()} {suffix}")
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
        result.append({"path": str(path), "content": read_text_exact(path) if path.exists() else ""})
    return result


def _initialize_metadata(path: Path) -> None:
    write_text(path / "meta.ini", "modid=0\r\nversion=\r\nnewestVersion=\r\ncategory=0\r\ninstallationFile=\r\n")


def _normalize_color(color: str) -> str:
    qsettings.encode_qcolor(color)
    return "#" + color.strip().lstrip("#").lower()


def get_color(path: Path) -> str | None:
    meta = path / "meta.ini"
    if not meta.is_file():
        return None
    for line in read_text_exact(meta).splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "color":
            return qsettings.decode_qcolor(value)
    return None


def set_color(path: Path, color: str) -> bool:
    """Write the separator colour into ``meta.ini``; returns False when unchanged."""
    value = qsettings.encode_qcolor(color)
    meta = path / "meta.ini"
    if not meta.is_file():
        _initialize_metadata(path)
    text = read_text_exact(meta)
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    new_line = f"color={value}"
    for index, line in enumerate(lines):
        if line.partition("=")[0].strip() == "color":
            if line == new_line:
                return False
            lines[index] = new_line
            break
    else:
        general = next((i for i, line in enumerate(lines) if line.strip().casefold() == "[general]"), None)
        lines.insert(general + 1 if general is not None else len(lines), new_line)
    write_text(meta, newline.join(lines) + newline)
    return True


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


def create(instance: Instance, profile: str | None, name: str, before: str | None = None, after: str | None = None, all_profiles: bool = False, color: str | None = None) -> dict[str, object]:
    if color is not None:
        qsettings.encode_qcolor(color)  # validate before touching anything
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
    if color is not None:
        set_color(path, color)
    try:
        for profile_name in profiles:
            profile_path = instance.profiles_dir / profile_name
            model = ModList.read(profile_path / "modlist.txt")
            if model.find(internal) is not None:
                raise Mo2Error(f"Separator already exists in profile: {_display_name(internal)}")
            model.add(internal, enabled=True)
            _place(model, internal, before, after)
            write_text(profile_path / "modlist.txt", model.render())
        entry = record(instance, "separator_create", path=str(path), created_path=True, profiles=backups)
    except Exception:
        for backup in backups:
            write_text(Path(backup["path"]), backup["content"])
        shutil.rmtree(path, ignore_errors=True)
        raise
    return {"separator": _display_name(internal), "internal_name": internal, "path": str(path), "profiles": profiles, "color": color and _normalize_color(color), "journal_id": entry["id"]}


def parse_separator_list(text: str) -> list[tuple[str, str | None]]:
    """Parse an ordered separator list: one ``Name`` or ``Name | #RRGGBB`` per line.

    Blank lines and lines starting with ``;`` or ``//`` are ignored. The order is
    the order shown in MO2's mod list, top to bottom.
    """
    result: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(";") or line.startswith("//"):
            continue
        name, bar, color_text = line.partition("|")
        name = _display_name(name.strip())
        color = color_text.strip() or None
        if not name:
            raise Mo2Error(f"Separator list line {number} has no name.")
        if bar and color is None:
            raise Mo2Error(f"Separator list line {number} has an empty colour.")
        if color is not None:
            try:
                color = _normalize_color(color)
            except ValueError as error:
                raise Mo2Error(f"Separator list line {number}: {error}") from error
        _safe_name(name)
        if name.casefold() in seen:
            raise Mo2Error(f"Separator list line {number} repeats '{name}'.")
        seen.add(name.casefold())
        result.append((name, color))
    if not result:
        raise Mo2Error("Separator list is empty.")
    return result


def apply_list(instance: Instance, profile: str | None, list_file: str | Path, dry_run: bool = False) -> dict[str, object]:
    """Create missing separators, set colours and order them as listed. Safe to repeat.

    Separators are only moved when they are out of order relative to the previous
    listed separator; mods between them stay where they are.
    """
    path = Path(list_file).expanduser()
    if not path.is_file():
        raise Mo2Error(f"Separator list not found: {path}")
    wanted = parse_separator_list(path.read_text(encoding="utf-8-sig"))
    selected = instance.profile_name(profile)
    modlist_path = instance.profiles_dir / selected / "modlist.txt"
    model = ModList.read(modlist_path)
    created: list[str] = []
    recolored: list[str] = []
    moved: list[str] = []
    actual: list[str] = []
    for name, color in wanted:
        entry = _find_separator_entry(model, name)
        directory = _find_separator_path(instance, name)
        if entry is None:
            created.append(name)
            model.add(internal_name(name), enabled=True)
            entry = model.find(internal_name(name))
        actual.append(entry.name)
        if color is not None and (directory is None or get_color(directory) != color):
            recolored.append(name)
    # MO2 writes modlist.txt in reverse of the visible order: a separator shown
    # below another one must come before it in the file.
    for previous, current in zip(actual, actual[1:]):
        entries = model.entries
        previous_index = next(i for i, e in enumerate(entries) if e.name.casefold() == previous.casefold())
        current_index = next(i for i, e in enumerate(entries) if e.name.casefold() == current.casefold())
        if current_index > previous_index:
            model.move(current, previous_index)
            moved.append(_display_name(current))
    result: dict[str, object] = {
        "profile": selected,
        "separators": len(wanted),
        "created": created,
        "recolored": recolored,
        "moved": moved,
        "changed": bool(created or recolored or moved),
    }
    if dry_run:
        result["dry_run"] = True
        return result
    if not result["changed"]:
        return result
    profiles = _backups(instance, [selected])
    meta_states = []
    for name, _ in wanted:
        directory = _find_separator_path(instance, name)
        if directory is not None and (directory / "meta.ini").is_file():
            meta_states.append({"path": str(directory / "meta.ini"), "exists": True, "content": read_text_exact(directory / "meta.ini")})
    created_dirs = []
    for name in created:
        instance.mods_dir.mkdir(parents=True, exist_ok=True)
        directory = instance.mods_dir / internal_name(name)
        directory.mkdir(parents=False, exist_ok=True)
        _initialize_metadata(directory)
        created_dirs.append(str(directory))
    for name, color in wanted:
        if color is not None and name in recolored:
            set_color(_find_separator_path(instance, name), color)
    write_text(modlist_path, model.render())
    entry = record(
        instance,
        "separator_group",
        separator="(apply list)",
        profiles=profiles,
        state_files=meta_states,
        names=[name for name, _ in wanted],
        created_dirs=created_dirs,
    )
    result["journal_id"] = entry["id"]
    return result


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
    try:
        model.add(actual_internal, enabled=True)
        _place(model, actual_internal, before, after)
        write_text(profile_path / "modlist.txt", model.render())
        entry = record(instance, "separator_create", path=str(path), created_path=False, profiles=backup)
    except Exception:
        for item in backup:
            write_text(Path(item["path"]), item["content"])
        raise
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
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    trash = instance.base / ".mo2cli-trash" / f"{path.name}-{stamp}"
    trash.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(trash))
    try:
        for profile_name in profiles:
            profile_path = instance.profiles_dir / profile_name
            model = ModList.read(profile_path / "modlist.txt")
            model.lines = [line for line in model.lines if not (hasattr(line, "name") and line.name.casefold() == internal.casefold())]
            write_text(profile_path / "modlist.txt", model.render())
        entry = record(instance, "separator_remove", reversible=not purge, path=str(instance.mods_dir / internal), trash=str(trash), profiles=backups)
    except Exception:
        if trash.exists() and not path.exists():
            shutil.move(str(trash), str(path))
        for backup in backups:
            write_text(Path(backup["path"]), backup["content"])
        raise
    if purge:
        shutil.rmtree(trash)
        trash = None
    return {"removed": _display_name(internal), "internal_name": internal, "purged": purge, "trash": str(trash) if trash else None, "journal_id": entry["id"]}


def group(instance: Instance, profile: str | None, separator: str, names: list[str]) -> dict[str, object]:
    if not names:
        raise Mo2Error("At least one mod required for group.")
    profile_name = instance.profile_name(profile)
    profile_path = instance.profiles_dir / profile_name
    modlist_path = profile_path / "modlist.txt"
    backup = _backups(instance, [profile_name])
    model = ModList.read(modlist_path)
    separator_entry = _find_separator_entry(model, separator)
    if separator_entry is None:
        internal = internal_name(separator)
        raise Mo2Error(f"Separator not found: {_display_name(internal)}")
    internal = separator_entry.name
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
    try:
        write_text(modlist_path, model.render())
        entry = record(instance, "separator_group", separator=internal, profiles=backup, names=names)
    except Exception:
        for item in backup:
            write_text(Path(item["path"]), item["content"])
        raise
    return {"separator": _display_name(internal), "profile": profile_name, "mods": names, "journal_id": entry["id"]}
