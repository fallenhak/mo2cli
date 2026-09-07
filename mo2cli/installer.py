from __future__ import annotations

import datetime as dt
import shutil
import sys
from pathlib import Path
from typing import Any

from .archives import archive_stem, extract_to_temp
from .formats import ModList, write_text
from .fomod import apply_plan, config_hash, module_name, plan_extracted, public_plan, reconcile_selections
from .fomod_decisions import archive_fingerprint, context_snapshot, find as find_fomod_decision, save as save_fomod_decision
from .journal import record
from .metadata import IniDocument, ModMetadata
from .workspace import Instance, Mo2Error, _safe_name


_GAME_DATA_DIRECTORIES = {
    "calientetools",
    "dyndolod",
    "interface",
    "meshes",
    "music",
    "root",
    "scripts",
    "seq",
    "skse",
    "sound",
    "strings",
    "textures",
    "video",
}


def _metadata_game_name(instance: Instance) -> str:
    """Use MO2's short game id, not its display name, in mod metadata."""
    if instance.mods_dir.is_dir():
        for mod in instance.mods_dir.iterdir():
            meta = mod / "meta.ini"
            if not meta.is_file():
                continue
            value = ModMetadata.read(mod).get("gameName")
            if isinstance(value, str) and value.strip():
                return value.strip()
    known_names = {
        "skyrim special edition": "SkyrimSE",
        "skyrim special edition (steam)": "SkyrimSE",
    }
    return known_names.get(instance.game_name.casefold(), instance.game_name)


def _mark_download_installed(archive: Path) -> None:
    """Keep MO2's Downloads tab in sync with a CLI installation."""
    meta_path = Path(f"{archive}.meta")
    if not meta_path.is_file():
        return
    try:
        document = IniDocument.read(meta_path)
        document.set("installed", True, section="General")
        document.set("uninstalled", False, section="General")
        document.write(meta_path)
    except OSError:
        # The mod is already installed; a locked sidecar must not roll it back.
        return


def _ask_reuse_fomod(saved: dict[str, Any], selections: dict[str, list[str]], changed: bool, dropped: list[str], context_changed: bool) -> bool:
    print("Saved FOMOD decision found:")
    for group, plugins in selections.items():
        if plugins:
            print(f"  {group}: {', '.join(plugins)}")
    if changed:
        print("  FOMOD configuration has changed; matching selections will be reused.")
    if dropped:
        print("  Selections no longer available: " + ", ".join(dropped))
    if context_changed:
        print("  Active mod context in profile has changed.")
    answer = input("Reuse these selections? [Y/n]: ").strip().casefold()
    return answer not in {"n", "no"}


def _decision_payload(
    instance: Instance,
    profile: str | None,
    archive: Path,
    config_digest: str,
    plan: dict[str, object],
    mod_name: str,
    flags: dict[str, str] | None,
    game_version: str | None,
    separator: str | None,
    source: str,
    warnings: list[str],
) -> dict[str, Any]:
    return {
        "archive_name": archive.name,
        "archive_sha256": archive_fingerprint(archive),
        "config_sha256": config_digest,
        "module_name": str(plan.get("module_name", "")),
        "mod_name": mod_name,
        "selections": plan.get("selections", {}),
        "selected": plan.get("selected", []),
        "flags": dict(flags or {}),
        "game_version": game_version,
        "separator": separator,
        "context": context_snapshot(instance, profile),
        "source": source,
        "warnings": warnings,
    }


def _source_root(extracted: Path, explicit: str | None = None) -> Path:
    if explicit:
        source = extracted.joinpath(*Path(explicit).parts)
        if not source.is_dir() or not source.resolve().is_relative_to(extracted.resolve()):
            raise Mo2Error(f"Source folder inside archive not found: {explicit}")
        return source
    root = extracted
    # MO2's simple installer convention: a Data folder is the mod root.
    data = root / "Data"
    if data.is_dir():
        root = data
    while True:
        children = [item for item in root.iterdir() if item.name.casefold() not in {"__macosx"}]
        directories = [item for item in children if item.is_dir()]
        files = [item for item in children if item.is_file()]
        # Once a recognized Skyrim data directory is visible, keep it as the
        # mod root.  Otherwise Data\textures\sky would be flattened into the
        # mod root and MO2 would correctly report invalid game data.
        if any(item.name.casefold() in _GAME_DATA_DIRECTORIES for item in directories):
            break
        if len(directories) == 1 and not files and directories[0].name.casefold() not in {"fomod", "data"}:
            root = directories[0]
            continue
        if len(directories) == 1 and not files and directories[0].name.casefold() == "data":
            root = directories[0]
        break
    return root


def _copy_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.rglob("*"):
        if item.is_symlink():
            raise Mo2Error(f"Symlinks inside archive are not supported: {item}")
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def _has_fomod(root: Path) -> bool:
    # A few tool archives contain only FOMod/info.xml as package metadata;
    # that is not an interactive installer.  A real FOMOD selection module
    # has ModuleConfig.xml, so use that as the authoritative marker.
    return any(path.is_file() and path.name.casefold() == "moduleconfig.xml" for path in root.rglob("*"))


def _trash_path(instance: Instance, name: str) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return instance.base / ".mo2cli-trash" / f"{name}-{stamp}"


def install_archive(
    instance: Instance,
    archive: str | Path,
    profile: str | None = None,
    name: str | None = None,
    disabled: bool = False,
    replace: bool = False,
    allow_fomod: bool = False,
    source: str | None = None,
    fomod_selections: dict[str, list[str]] | None = None,
    fomod_flags: dict[str, str] | None = None,
    fomod_game_version: str | None = None,
    dry_run: bool = False,
    metadata_updates: dict[str, object] | None = None,
    separator: str | None = None,
    fomod_reuse: str = "auto",
) -> dict[str, object]:
    archive_path = Path(archive).expanduser().resolve()
    if not archive_path.is_file():
        raise Mo2Error(f"Archive not found: {archive_path}")
    mod_name = name or archive_stem(archive_path)
    extracted = extract_to_temp(archive_path)
    moved_to: Path | None = None
    destination: Path | None = None
    decision_payload: dict[str, Any] | None = None
    try:
        has_fomod = _has_fomod(extracted.root)
        if has_fomod and not allow_fomod:
            raise Mo2Error("Archive contains FOMOD; choices cannot be made automatically. Use --allow-fomod to proceed.")
        plan = None
        if has_fomod:
            file_states = {str(item["path"]): "Active" for item in instance.virtual_files(profile)}
            effective_selections = fomod_selections
            decision_source = "new"
            decision_warnings: list[str] = []
            config_digest = config_hash(extracted.root)
            current_module_name = module_name(extracted.root)
            explicit_choices = bool(fomod_selections or fomod_flags or fomod_game_version)
            if not explicit_choices and fomod_reuse != "never":
                exact, compatible = find_fomod_decision(instance, profile, archive_path, current_module_name, config_digest)
                saved = exact or compatible
                if saved:
                    saved_selections = saved.get("selections", {})
                    if not isinstance(saved_selections, dict):
                        saved_selections = {}
                    saved_selections = {str(key): [str(item) for item in value] for key, value in saved_selections.items() if isinstance(value, list)}
                    reconciled, dropped = reconcile_selections(extracted.root, saved_selections)
                    current_context = context_snapshot(instance, profile)
                    context_changed = saved.get("context") != current_context
                    changed = exact is None
                    reuse = fomod_reuse == "always" or dry_run
                    if fomod_reuse == "auto" and not dry_run and sys.stdin.isatty():
                        reuse = _ask_reuse_fomod(saved, reconciled, changed, dropped, context_changed)
                    if reuse:
                        effective_selections = reconciled
                        decision_source = "saved" if not changed and not dropped else "reconciled"
                        if changed:
                            decision_warnings.append("FOMOD configuration changed after previous record.")
                        if dropped:
                            decision_warnings.append("Some saved FOMOD selections were skipped as they no longer exist.")
                        if context_changed:
                            decision_warnings.append("Active mod context during recording has changed.")
                    else:
                        decision_source = "saved-declined"
            plan = plan_extracted(extracted.root, selections=effective_selections, flags=fomod_flags, file_states=file_states, game_version=fomod_game_version)
            if decision_source == "reconciled" and plan.get("errors"):
                fallback = plan_extracted(extracted.root, selections=None, flags=fomod_flags, file_states=file_states, game_version=fomod_game_version)
                if not fallback.get("errors"):
                    plan = fallback
                    decision_source = "reconciled-defaults"
                    decision_warnings.append("Used current defaults due to mismatched selections.")
            if not plan.get("errors") and not plan.get("operations"):
                raise Mo2Error("FOMOD selection produced no game files; empty mod not installed.")
            suggested_name = str(plan.get("module_name") or "").strip()
            if name is None and suggested_name:
                mod_name = suggested_name
            decision_payload = _decision_payload(instance, profile, archive_path, config_digest, plan, mod_name, fomod_flags, fomod_game_version, separator, decision_source, decision_warnings)
        else:
            root = _source_root(extracted.root, source)
        _safe_name(mod_name)
        destination = instance.mods_dir / mod_name
        existing = next((path for path in instance.mods_dir.iterdir() if path.name.casefold() == mod_name.casefold()), None) if instance.mods_dir.exists() else None
        if existing and not replace:
            raise Mo2Error(f"Mod already exists: {existing.name}; use --replace to overwrite.")
        if dry_run:
            result: dict[str, object] = {"dry_run": True, "name": mod_name, "archive": str(archive_path), "fomod": has_fomod, "replaced": bool(existing), "enabled": not disabled}
            if plan is not None:
                result["plan"] = public_plan(plan)
                if decision_payload is not None:
                    result["decision"] = decision_payload
            else:
                result["source"] = str(root.relative_to(extracted.root))
            return result
        profile_path = instance.profile_path(profile)
        modlist_path = profile_path / "modlist.txt"
        previous_modlist = modlist_path.read_text(encoding="utf-8") if modlist_path.exists() else ""
        if existing:
            moved_to = _trash_path(instance, existing.name)
            moved_to.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(existing), str(moved_to))
        if plan is not None:
            apply_plan(plan, destination)
        else:
            _copy_tree(root, destination)
        metadata = ModMetadata.read(destination)
        metadata.update({"installationFile": archive_path.name, "gameName": _metadata_game_name(instance)})
        if metadata_updates:
            metadata.update(metadata_updates)
        modlist = ModList.read(modlist_path)
        old = modlist.find(mod_name)
        if old:
            modlist.set_enabled(mod_name, not disabled)
        else:
            modlist.add(mod_name, enabled=not disabled)
        write_text(profile_path / "modlist.txt", modlist.render())
        from .plugins import sync_plugin_lists

        sync_plugin_lists(instance, profile)
        _mark_download_installed(archive_path)
        if decision_payload is not None:
            save_fomod_decision(instance, profile, decision_payload)
        journal_entry = record(instance, "install", destination=str(destination), replaced=str(moved_to) if moved_to else None, profiles=[{"path": str(modlist_path), "content": previous_modlist}])
        result = {"name": mod_name, "path": str(destination), "archive": str(archive_path), "fomod": has_fomod, "replaced": str(moved_to) if moved_to else None, "enabled": not disabled, "selected": plan.get("selected", []) if plan else None, "journal_id": journal_entry["id"]}
        if decision_payload is not None:
            result["decision"] = decision_payload
        return result
    except Exception:
        if destination and destination.exists():
            shutil.rmtree(destination)
        if moved_to and moved_to.exists():
            shutil.move(str(moved_to), str(destination))
        raise
    finally:
        extracted.close()


def remove_mod(instance: Instance, name: str, purge: bool = False, yes: bool = False) -> dict[str, object]:
    if not yes:
        raise Mo2Error("Mod removal requires --yes.")
    _safe_name(name)
    path = next((item for item in instance.mods_dir.iterdir() if item.name.casefold() == name.casefold()), None)
    if path is None or not path.is_dir():
        raise Mo2Error(f"Mod not found: {name}")
    profile_backups = []
    for profile_name in instance.list_profiles():
        modlist_path = instance.profiles_dir / profile_name / "modlist.txt"
        profile_backups.append({"path": str(modlist_path), "content": modlist_path.read_text(encoding="utf-8") if modlist_path.exists() else ""})
    moved_to = None
    if purge:
        shutil.rmtree(path)
    else:
        moved_to = _trash_path(instance, path.name)
        moved_to.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(moved_to))
    for profile_name in instance.list_profiles():
        profile = instance.profiles_dir / profile_name
        modlist = ModList.read(profile / "modlist.txt")
        modlist.lines = [line for line in modlist.lines if not (hasattr(line, "name") and line.name.casefold() == name.casefold())]
        write_text(profile / "modlist.txt", modlist.render())
    journal_entry = record(instance, "remove", reversible=not purge, destination=str(instance.mods_dir / path.name), trash=str(moved_to) if moved_to else None, profiles=profile_backups)
    return {"removed": name, "purged": purge, "trash": str(moved_to) if moved_to else None, "journal_id": journal_entry["id"]}


def rename_mod(instance: Instance, old: str, new: str) -> dict[str, object]:
    _safe_name(old)
    _safe_name(new)
    source = next((item for item in instance.mods_dir.iterdir() if item.name.casefold() == old.casefold()), None)
    if source is None or not source.is_dir():
        raise Mo2Error(f"Mod not found: {old}")
    target = instance.mods_dir / new
    if target.exists():
        raise Mo2Error(f"Target mod name already exists: {new}")
    profile_backups = []
    for profile_name in instance.list_profiles():
        modlist_path = instance.profiles_dir / profile_name / "modlist.txt"
        profile_backups.append({"path": str(modlist_path), "content": modlist_path.read_text(encoding="utf-8") if modlist_path.exists() else ""})
    source.rename(target)
    for profile_name in instance.list_profiles():
        profile = instance.profiles_dir / profile_name
        modlist = ModList.read(profile / "modlist.txt")
        if modlist.find(old):
            modlist.rename(old, new)
            write_text(profile / "modlist.txt", modlist.render())
    journal_entry = record(instance, "rename", old_path=str(source), new_path=str(target), profiles=profile_backups)
    return {"old": old, "new": new, "path": str(target), "journal_id": journal_entry["id"]}
