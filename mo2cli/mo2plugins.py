"""Install, list and remove Mod Organizer 2 *plugins* (not game plugins).

MO2 plugins (Root Builder, FOMOD Plus, Python/DLL plugins) live in
``<MO2>\\plugins``. They are not mods, so ``mods install`` cannot place them.
Every installation is recorded in ``<base>\\.mo2cli\\mo2plugins.json`` and in the
transaction journal so that ``undo`` and ``mo2plugin remove`` know exactly which
files belong to which archive.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path, PurePosixPath

from .archives import _safe_member, archive_stem, extract_to_temp, list_archive, sha256
from .downloads import write_meta
from .formats import read_text_exact, write_text
from .journal import record
from .workspace import Instance, Mo2Error

# Top-level files accepted when an archive has no ``plugins`` or plugin folder.
_LOOSE_SUFFIXES = {".py", ".dll", ".pyd", ".json", ".ini"}
_LOOSE_REQUIRED_SUFFIXES = {".py", ".dll", ".pyd"}


def plugins_dir(instance: Instance) -> Path:
    base = instance.base
    if not ((base / "ModOrganizer.exe").is_file() or (base / "plugins").is_dir()):
        raise Mo2Error(
            "MO2 plugins can only be managed for a portable instance whose folder holds ModOrganizer.exe "
            f"or a plugins folder: {base}"
        )
    return base / "plugins"


def manifest_path(instance: Instance) -> Path:
    return instance.base / ".mo2cli" / "mo2plugins.json"


def read_manifest(instance: Instance) -> dict[str, dict[str, object]]:
    path = manifest_path(instance)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise Mo2Error(f"MO2 plugin manifest is not valid JSON: {path}") from error
    plugins = data.get("plugins") if isinstance(data, dict) else None
    return plugins if isinstance(plugins, dict) else {}


def _write_manifest(instance: Instance, plugins: dict[str, dict[str, object]]) -> None:
    write_text(manifest_path(instance), json.dumps({"version": 1, "plugins": plugins}, indent=2, ensure_ascii=False) + "\n")


def _snapshot(path: Path) -> dict[str, object]:
    return {"path": str(path), "exists": path.is_file(), "content": read_text_exact(path) if path.is_file() else ""}


def _restore_snapshot(item: dict[str, object]) -> None:
    path = Path(str(item["path"]))
    if item.get("exists"):
        write_text(path, str(item.get("content", "")))
    elif path.exists():
        path.unlink()


def _parts(name: str) -> tuple[str, ...]:
    return PurePosixPath(name.replace("\\", "/")).parts


def plan_layout(members: list[str], source: str | None = None) -> dict[str, object]:
    """Decide where each archive file goes below ``plugins``.

    Recognised layouts, in this order:

    * ``plugins-dir``: a top-level ``plugins`` folder (optionally inside one
      wrapper folder); its contents go into ``plugins``.
    * ``plugin-folder``: everything lives in a single folder such as
      ``rootbuilder``; that folder goes into ``plugins``.
    * ``loose``: top-level ``.py``/``.dll`` files (plus folders) go into ``plugins``.

    ``source`` names an archive subfolder to treat as the archive root.
    """
    entries = [(member, _parts(member)) for member in members]
    if source:
        prefix = tuple(part.casefold() for part in _parts(source))
        entries = [
            (member, parts[len(prefix):])
            for member, parts in entries
            if len(parts) > len(prefix) and tuple(part.casefold() for part in parts[: len(prefix)]) == prefix
        ]
        if not entries:
            raise Mo2Error(f"Source folder not found inside the archive: {source}")
    if not entries:
        raise Mo2Error("The archive contains no files.")
    return _plan_entries(entries, 0)


def _plan_entries(entries: list[tuple[str, tuple[str, ...]]], depth: int) -> dict[str, object]:
    plugins_entries = [(m, p) for m, p in entries if len(p) > 1 and p[0].casefold() == "plugins"]
    if plugins_entries:
        selected = {m for m, _ in plugins_entries}
        return {
            "kind": "plugins-dir",
            "mapping": [(m, "/".join(p[1:])) for m, p in plugins_entries],
            "ignored": [m for m, _ in entries if m not in selected],
            "default_name": None,
        }
    tops = {p[0].casefold() for _, p in entries}
    if len(tops) == 1 and all(len(p) > 1 for _, p in entries):
        inner = [(m, p[1:]) for m, p in entries]
        if depth == 0 and any(len(p) > 1 and p[0].casefold() == "plugins" for _, p in inner):
            plan = _plan_entries(inner, depth + 1)
            return plan
        return {
            "kind": "plugin-folder",
            "mapping": [(m, "/".join(p)) for m, p in entries],
            "ignored": [],
            "default_name": entries[0][1][0],
        }
    top_files = [(m, p) for m, p in entries if len(p) == 1]
    nested = [(m, p) for m, p in entries if len(p) > 1]
    plugin_files = [(m, p) for m, p in top_files if PurePosixPath(p[0]).suffix.casefold() in _LOOSE_REQUIRED_SUFFIXES]
    if not plugin_files:
        raise Mo2Error(
            "Cannot tell where this archive's plugin files go: expected a 'plugins' folder, a single plugin "
            "folder, or top-level .py/.dll files. Use --source SUBDIR to point at the right folder."
        )
    keep_files = [(m, p) for m, p in top_files if PurePosixPath(p[0]).suffix.casefold() in _LOOSE_SUFFIXES]
    kept = keep_files + nested
    selected = {m for m, _ in kept}
    return {
        "kind": "loose",
        "mapping": [(m, "/".join(p)) for m, p in kept],
        "ignored": [m for m, _ in entries if m not in selected],
        "default_name": None,
    }


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _trash(instance: Instance, label: str) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return instance.base / ".mo2cli-trash" / f"{label}-{stamp}"


def _archive_in_downloads(instance: Instance, archive: Path) -> Path:
    downloads = instance.downloads_dir
    target = downloads / archive.name
    if archive.resolve() == target.resolve():
        return target
    if target.exists():
        if sha256(target) != sha256(archive):
            raise Mo2Error(f"Downloads already contain a different file named {archive.name}; rename the archive first.")
        return target
    downloads.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive, target)
    return target


def _remove_empty_parents(path: Path, stop: Path) -> list[Path]:
    removed = []
    current = path.parent
    while current != stop and _inside(current, stop) and current.is_dir() and not any(current.iterdir()):
        current.rmdir()
        removed.append(current)
        current = current.parent
    return removed


def _move_files_to_trash(plugin_root: Path, relatives: list[str], trash_root: Path) -> tuple[list[dict[str, str]], list[str]]:
    backups: list[dict[str, str]] = []
    removed_dirs: list[str] = []
    for relative in relatives:
        path = plugin_root.joinpath(*_parts(relative))
        if not _inside(path, plugin_root):
            continue
        if not path.is_file():
            removed_dirs.extend(str(item) for item in _remove_empty_parents(path, plugin_root))
            continue
        destination = trash_root.joinpath(*_parts(relative))
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(destination))
        backups.append({"path": str(path), "trash": str(destination)})
        removed_dirs.extend(str(item) for item in _remove_empty_parents(path, plugin_root))
    return backups, removed_dirs


def install(
    instance: Instance,
    archive: str | Path,
    name: str | None = None,
    source: str | None = None,
    replace: bool = False,
    dry_run: bool = False,
    direct_url: str | None = None,
) -> dict[str, object]:
    archive_path = Path(archive).expanduser().resolve()
    if not archive_path.is_file():
        raise Mo2Error(f"Archive not found: {archive_path}")
    root = plugins_dir(instance)
    records = list_archive(archive_path)
    members = []
    for item in records:
        _safe_member(str(item["name"]))
        if not item.get("directory"):
            members.append(str(item["name"]))
    plan = plan_layout(members, source)
    plugin_name = (name or str(plan["default_name"] or "") or archive_stem(archive_path)).strip()
    if not plugin_name:
        raise Mo2Error("Could not determine a plugin name; use --name.")
    mapping: list[tuple[str, str]] = list(plan["mapping"])  # type: ignore[arg-type]
    destinations = [relative for _, relative in mapping]
    for relative in destinations:
        _safe_member(relative)

    manifest = read_manifest(instance)
    previous = next((value for key, value in manifest.items() if key.casefold() == plugin_name.casefold()), None)
    previous_key = next((key for key in manifest if key.casefold() == plugin_name.casefold()), None)
    if previous is not None and not replace:
        raise Mo2Error(f"MO2 plugin already installed: {plugin_name}; use --replace to reinstall.")
    owned = set(str(item).casefold() for item in (previous or {}).get("files", []))  # type: ignore[union-attr]
    conflicts = []
    for relative in destinations:
        target = root.joinpath(*_parts(relative))
        if target.exists() and relative.casefold() not in owned:
            conflicts.append(relative)
    if conflicts and not replace:
        sample = ", ".join(conflicts[:5])
        raise Mo2Error(f"{len(conflicts)} file(s) already exist in the plugins folder ({sample}); use --replace to move them to the trash.")

    result: dict[str, object] = {
        "name": plugin_name,
        "archive": str(archive_path),
        "layout": plan["kind"],
        "plugins_dir": str(root),
        "files": len(destinations),
        "ignored": plan["ignored"],
        "replaces_existing": previous is not None,
        "overwrites": conflicts,
    }
    if dry_run:
        result["dry_run"] = True
        result["sample"] = sorted(destinations, key=str.casefold)[:10]
        return result

    downloads_archive = _archive_in_downloads(instance, archive_path)
    meta_path = downloads_archive.with_name(downloads_archive.name + ".meta")
    manifest_snapshot = _snapshot(manifest_path(instance))
    state = [_snapshot(meta_path)]
    trash_root = _trash(instance, f"mo2plugin-{plugin_name}")
    backups: list[dict[str, str]] = []
    removed_dirs: list[str] = []
    created_dirs: list[Path] = []
    copied: list[Path] = []
    staged = extract_to_temp(archive_path)
    try:
        # Files of a previous installation of the same plugin, then foreign
        # files that collide with the new ones, are moved aside, not deleted.
        stale = [str(item) for item in (previous or {}).get("files", []) if str(item).casefold() not in {d.casefold() for d in destinations}]  # type: ignore[union-attr]
        first, dirs_a = _move_files_to_trash(root, stale + conflicts + [d for d in destinations if d.casefold() in owned], trash_root)
        backups.extend(first)
        removed_dirs.extend(dirs_a)
        for member, relative in mapping:
            source_file = staged.root.joinpath(*_parts(member))
            target = root.joinpath(*_parts(relative))
            if not _inside(target, root):
                raise Mo2Error(f"Refusing to write outside the plugins folder: {relative}")
            parent = target.parent
            missing = []
            while not parent.exists() and parent != root:
                missing.append(parent)
                parent = parent.parent
            target.parent.mkdir(parents=True, exist_ok=True)
            created_dirs.extend(reversed(missing))
            shutil.copy2(source_file, target)
            copied.append(target)
        meta_values = write_meta(meta_path, name=downloads_archive.name, mod_name=plugin_name, installed=True, direct_url=direct_url, defaults={"removed": False})
        record_data = {
            "archive": downloads_archive.name,
            "archive_sha256": sha256(downloads_archive),
            "layout": plan["kind"],
            "installed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "files": sorted(destinations, key=str.casefold),
        }
        if previous_key is not None:
            manifest.pop(previous_key)
        manifest[plugin_name] = record_data
        _write_manifest(instance, manifest)
        entry = record(
            instance,
            "mo2plugin_install",
            name=plugin_name,
            plugins_dir=str(root),
            files=[str(path) for path in copied],
            created_dirs=[str(path) for path in created_dirs],
            backups=backups,
            removed_dirs=removed_dirs,
            manifest_before=manifest_snapshot,
            state_files=state,
        )
    except Exception:
        for path in copied:
            if path.exists():
                path.unlink()
        for path in reversed(created_dirs):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        for item in backups:
            original, saved = Path(item["path"]), Path(item["trash"])
            if saved.exists() and not original.exists():
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(saved), str(original))
        _restore_snapshot(manifest_snapshot)
        for item in state:
            _restore_snapshot(item)
        raise
    finally:
        staged.close()
    result.update({
        "downloads_archive": str(downloads_archive),
        "meta": str(meta_path),
        "wabbajack_source": next((key for key in ("directURL", "manualURL") if meta_values.get(key)), None) or ("nexus" if meta_values.get("modID") else None),
        "journal_id": entry["id"],
    })
    if result["wabbajack_source"] is None:
        result["note"] = "No Wabbajack source yet: run 'mo2 downloads meta <archive> --direct-url|--nexus ...'."
    return result


def list_installed(instance: Instance) -> list[dict[str, object]]:
    root = instance.base / "plugins"
    result = []
    for name, value in sorted(read_manifest(instance).items(), key=lambda item: item[0].casefold()):
        files = [str(item) for item in value.get("files", [])]
        missing = [item for item in files if not root.joinpath(*_parts(item)).is_file()]
        result.append({
            "name": name,
            "archive": value.get("archive"),
            "layout": value.get("layout"),
            "files": len(files),
            "missing_files": len(missing),
            "installed_at": value.get("installed_at"),
        })
    return result


def remove(instance: Instance, name: str, dry_run: bool = False) -> dict[str, object]:
    root = plugins_dir(instance)
    manifest = read_manifest(instance)
    key = next((item for item in manifest if item.casefold() == name.casefold()), None)
    if key is None:
        raise Mo2Error(f"MO2 plugin not managed by mo2cli: {name}")
    value = manifest[key]
    files = [str(item) for item in value.get("files", [])]
    present = [item for item in files if root.joinpath(*_parts(item)).is_file()]
    result: dict[str, object] = {"name": key, "files": len(present), "missing_files": len(files) - len(present)}
    if dry_run:
        result["dry_run"] = True
        return result
    manifest_snapshot = _snapshot(manifest_path(instance))
    meta_path = instance.downloads_dir / f"{value.get('archive')}.meta"
    state = [_snapshot(meta_path)]
    trash_root = _trash(instance, f"mo2plugin-remove-{key}")
    backups, removed_dirs = _move_files_to_trash(root, files, trash_root)
    try:
        del manifest[key]
        _write_manifest(instance, manifest)
        if meta_path.is_file():
            write_meta(meta_path, installed=False)
        entry = record(
            instance,
            "mo2plugin_remove",
            name=key,
            plugins_dir=str(root),
            backups=backups,
            removed_dirs=removed_dirs,
            manifest_before=manifest_snapshot,
            state_files=state,
        )
    except Exception:
        for item in backups:
            original, saved = Path(item["path"]), Path(item["trash"])
            if saved.exists() and not original.exists():
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(saved), str(original))
        _restore_snapshot(manifest_snapshot)
        for item in state:
            _restore_snapshot(item)
        raise
    result.update({"trash": str(trash_root), "journal_id": entry["id"]})
    return result


def installed_archive_names(instance: Instance) -> dict[str, str]:
    """Map casefolded archive file names to plugin names (for ``downloads sync``)."""
    try:
        manifest = read_manifest(instance)
    except Mo2Error:
        return {}
    return {str(value.get("archive", "")).casefold(): name for name, value in manifest.items() if value.get("archive")}


def has_plugin_markers(instance: Instance, *needles: str) -> bool:
    """True when any entry of ``<base>\\plugins`` contains all words of one needle."""
    root = instance.base / "plugins"
    if not root.is_dir():
        return False
    names = [item.name.casefold().replace("_", " ").replace("-", " ") for item in root.iterdir()]
    return any(all(word in name for word in needle.split()) for needle in needles for name in names)

