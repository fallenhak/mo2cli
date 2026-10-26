from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from .downloads import fetch
from .formats import ModList, read_text_exact, write_text
from .installer import install_archive
from .journal import undo_last
from .github import fetch_asset as fetch_github_asset
from .github import is_github_ref
from .nexus import NexusReference, download_reference, game_domain, parse_reference
from .separators import ensure as ensure_separator
from .separators import group as group_separator
from .workspace import Instance, Mo2Error


def _integer(value: object, label: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise Mo2Error(f"Manifest {label} invalid: {value}") from error


def load(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise Mo2Error(f"Manifest file not found: {source}")
    try:
        if source.suffix.casefold() == ".toml":
            value = tomllib.loads(source.read_text(encoding="utf-8-sig"))
        else:
            value = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
        raise Mo2Error(f"Failed to parse manifest: {error}") from error
    if not isinstance(value, dict):
        raise Mo2Error("Manifest root must be a JSON/TOML object.")
    return value


def _list(value: Any, key: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise Mo2Error(f"Manifest {key} field must be a list of objects.")
    return value


def _profiles_for_manifest(instance: Instance, profile: str | None, root: dict[str, Any]) -> str:
    selected = profile or root.get("profile")
    return instance.profile_name(str(selected) if selected else None)


def _remember_journal(result: dict[str, object], journal_ids: list[str]) -> None:
    journal_id = result.get("journal_id")
    if isinstance(journal_id, str):
        journal_ids.append(journal_id)


def _rollback(instance: Instance, journal_ids: list[str]) -> list[str]:
    errors: list[str] = []
    for expected in reversed(journal_ids):
        try:
            undone = undo_last(instance)
        except (Mo2Error, OSError) as error:
            errors.append(f"{expected}: {error}")
            break
        if undone.get("id") != expected:
            errors.append(f"Expected journal {expected}, but undo selected {undone.get('id')}.")
            break
    return errors


def apply(
    instance: Instance,
    manifest_path: str | Path,
    profile: str | None = None,
    nexus_api_key: str | None = None,
    dry_run: bool = False,
    replace: bool = False,
    continue_on_error: bool = False,
    auto_download: bool = False,
) -> dict[str, object]:
    source = Path(manifest_path).expanduser().resolve()
    root = load(source)
    selected_profile = _profiles_for_manifest(instance, profile, root)
    all_mod_specs = _list(root.get("mods", []), "mods")
    separator_specs = _list(root.get("separators", []), "separators")
    _validate_extras(root)
    # Entries without a source (exported with source_missing) and entries marked if_missing that are already installed are not installed again.
    present = {entry.name.casefold() for entry in instance.profile_files(selected_profile)[1].entries}
    mod_specs: list[dict[str, Any]] = []
    skipped: list[dict[str, object]] = []
    for item in all_mod_specs:
        name = str(item.get("name") or "")
        installed = bool(name) and (name.casefold() in present or (instance.mods_dir / name).is_dir())
        if (item.get("if_missing") and installed) or (item.get("source_missing") and not any(item.get(key) for key in ("path", "url", "nexus", "nxm", "source"))):
            skipped.append({"name": name, "present": installed, "reason": "already installed" if installed else "no source in manifest"})
        else:
            mod_specs.append(item)
    result: dict[str, object] = {"manifest": str(source), "profile": selected_profile, "dry_run": dry_run, "separators": [], "mods": [], "groups": [], "skipped": skipped}
    if dry_run:
        for item in separator_specs:
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                raise Mo2Error("Manifest separator requires a name.")
            if item.get("before") and item.get("after"):
                raise Mo2Error(f"Manifest separator cannot specify both before and after: {name}")
            if "mods" in item and not isinstance(item.get("mods"), list):
                raise Mo2Error(f"Manifest separator mods must be a list: {name}")
        dry_separators = [{"name": item.get("name"), "before": item.get("before"), "after": item.get("after")} for item in separator_specs]
        declared = {str(item.get("name")).casefold() for item in separator_specs if isinstance(item.get("name"), str)}
        for item in mod_specs:
            if item.get("name") is not None and not isinstance(item.get("name"), str):
                raise Mo2Error("Manifest mod name must be a string.")
            name = item.get("separator")
            if name is not None and not isinstance(name, str):
                raise Mo2Error("Manifest mod separator must be a string.")
            if name and not isinstance(item.get("name"), str):
                raise Mo2Error("Mod name required for manifest entries assigned to a separator.")
            if isinstance(name, str) and name.casefold() not in declared:
                dry_separators.append({"name": name, "before": None, "after": None, "implicit": True})
                declared.add(name.casefold())
            if "fomod" in item and not isinstance(item.get("fomod"), dict):
                raise Mo2Error("Manifest fomod field must be an object.")
            fomod = item.get("fomod") or {}
            if not isinstance(fomod.get("select", {}), dict) or not isinstance(fomod.get("flags", {}), dict):
                raise Mo2Error("Manifest fomod.select and fomod.flags must be objects.")

            local_path = item.get("path")
            if local_path is None and isinstance(item.get("source"), str) and not str(item["source"]).startswith(("http://", "https://")):
                local_path = item["source"]
            raw_source = item.get("source") or item.get("url") or item.get("nxm") or item.get("nexus") or item.get("path")
            if not raw_source:
                raise Mo2Error("Manifest mod entry requires path, url, nxm, or nexus.")
            preview: dict[str, object] = {
                "name": item.get("name"),
                "source": raw_source,
                "separator": item.get("separator"),
            }
            if local_path:
                archive = (source.parent / str(local_path)).resolve()
                preview["plan"] = install_archive(
                    instance,
                    archive,
                    selected_profile,
                    name=item.get("name"),
                    disabled=bool(item.get("disabled", False)),
                    replace=bool(item.get("replace", replace)),
                    allow_fomod="fomod" in item,
                    source=item.get("source_dir"),
                    fomod_selections=fomod.get("select"),
                    fomod_flags=fomod.get("flags"),
                    fomod_game_version=fomod.get("game_version"),
                    separator=item.get("separator"),
                    fomod_reuse="never",
                    dry_run=True,
                )
                preview["validated"] = True
            else:
                preview["validated"] = False
                preview["validation_pending"] = "Remote archive is not downloaded during dry-run."
            result["mods"].append(preview)
        result["separators"] = dry_separators
        result["groups"] = [{"separator": item.get("name"), "mods": item.get("mods", [])} for item in separator_specs if item.get("mods")]
        result["extras"] = {
            "separator_colors": [item["name"] for item in separator_specs if item.get("color")],
            "order": len(root.get("order") or []),
            "mo2_plugins": [item["name"] for item in root.get("mo2_plugins") or []],
            "settings": {section: len(values) for section, values in (root.get("settings") or {}).items()},
            "stock_game": root.get("stock_game"),
        }
        return result

    for spec in separator_specs:
        name = spec.get("name")
        if not isinstance(name, str) or not name.strip():
            raise Mo2Error("Manifest separator requires a name.")
        if spec.get("before") and spec.get("after"):
            raise Mo2Error(f"Manifest separator cannot specify both before and after: {name}")
        if "mods" in spec and not isinstance(spec.get("mods"), list):
            raise Mo2Error(f"Manifest separator mods must be a list: {name}")

    declared_separators = {
        str(item.get("name")).casefold()
        for item in separator_specs
        if isinstance(item.get("name"), str)
    }
    referenced_separators = {
        str(item.get("separator")).casefold()
        for item in mod_specs
        if isinstance(item.get("separator"), str)
    }
    journal_ids: list[str] = []
    for spec in separator_specs:
        name = spec.get("name")
        if not isinstance(name, str) or not name.strip():
            raise Mo2Error("Manifest separator requires a name.")
        try:
            created = ensure_separator(instance, selected_profile, name, spec.get("before"), spec.get("after"))
        except (Mo2Error, OSError) as error:
            rollback_errors = _rollback(instance, journal_ids)
            detail = f" Rollback incomplete: {'; '.join(rollback_errors)}" if rollback_errors else ""
            raise Mo2Error(f"Manifest separator setup failed ({name}): {error}.{detail}") from error
        result["separators"].append(created)
        _remember_journal(created, journal_ids)
    for name in sorted(referenced_separators - declared_separators):
        original = next((item.get("separator") for item in mod_specs if isinstance(item.get("separator"), str) and str(item.get("separator")).casefold() == name), None)
        if isinstance(original, str):
            try:
                created = ensure_separator(instance, selected_profile, original)
            except (Mo2Error, OSError) as error:
                rollback_errors = _rollback(instance, journal_ids)
                detail = f" Rollback incomplete: {'; '.join(rollback_errors)}" if rollback_errors else ""
                raise Mo2Error(f"Manifest separator setup failed ({original}): {error}.{detail}") from error
            result["separators"].append(created)
            _remember_journal(created, journal_ids)

    resolved_cache: dict[str, str] = {}
    if auto_download and not dry_run:
        urls_to_batch: list[str] = []
        for spec in mod_specs:
            raw = spec.get("nxm") or spec.get("nexus")
            if isinstance(raw, str) and ("nexusmods.com" in raw or raw.startswith("nxm://")):
                if "key=" not in raw:
                    urls_to_batch.append(raw)
            elif isinstance(raw, dict):
                url = raw.get("url")
                if url and isinstance(url, str) and "key=" not in url:
                    urls_to_batch.append(url)
                elif raw.get("mod_id") and raw.get("file_id"):
                    game = game_domain(instance, raw.get("game"))
                    urls_to_batch.append(f"https://www.nexusmods.com/{game}/mods/{raw['mod_id']}?tab=files&file_id={raw['file_id']}")

        if len(urls_to_batch) > 1:
            try:
                from .browser import batch_resolve_nxm_urls
                print(f"Pre-resolving {len(urls_to_batch)} Nexus downloads in parallel tabs...")
                resolved_cache = batch_resolve_nxm_urls(urls_to_batch)
            except Exception as batch_err:
                print(f"Batch resolution notice: {batch_err}. Falling back to sequential resolution.")

    errors: list[dict[str, object]] = []
    for spec in mod_specs:
        try:
            if spec.get("separator") and not isinstance(spec.get("name"), str):
                raise Mo2Error("Mod name required for manifest entries assigned to a separator.")
            archive: str | Path
            download_result = None
            local_path = spec.get("path")
            if local_path is None and isinstance(spec.get("source"), str) and not str(spec["source"]).startswith(("http://", "https://")):
                local_path = spec["source"]
            if local_path:
                archive = str((source.parent / str(local_path)).resolve())
            elif is_github_ref(str(spec.get("url") or "")):
                download_result = fetch_github_asset(instance, str(spec["url"]), spec.get("asset"), spec.get("output"), bool(spec.get("replace", replace)), spec.get("sha256"))
                archive = str(download_result["path"])
            elif spec.get("url") or (isinstance(spec.get("source"), str) and spec["source"].startswith(("http://", "https://"))):
                url = str(spec.get("url") or spec.get("source"))
                download_result = fetch(instance, url, spec.get("output"), bool(spec.get("replace", replace)), expected_sha256=spec.get("sha256"))
                archive = str(download_result["path"])
            elif spec.get("nxm") or spec.get("nexus"):
                nexus = spec.get("nexus") if isinstance(spec.get("nexus"), dict) else {}
                raw_reference = spec.get("nxm") or spec.get("nexus")
                if isinstance(raw_reference, dict):
                    if raw_reference.get("url"):
                        reference = parse_reference(str(raw_reference["url"]), instance, raw_reference.get("game"))
                    else:
                        game = game_domain(instance, raw_reference.get("game"))
                        mod_id = raw_reference.get("mod_id", raw_reference.get("mod"))
                        if mod_id is None:
                            raise Mo2Error("Manifest Nexus entry requires mod_id.")
                        raw_file = raw_reference.get("file_id", raw_reference.get("file"))
                        reference = NexusReference(game, _integer(mod_id, "Nexus mod_id"), _integer(raw_file, "Nexus file_id") if raw_file is not None and str(raw_file).strip().isdigit() else None)
                else:
                    reference = str(raw_reference)

                direct_url = None
                ref_lookup_key = str(reference) if isinstance(reference, str) else f"https://www.nexusmods.com/{reference.game}/mods/{reference.mod_id}?tab=files&file_id={reference.file_id}"
                if ref_lookup_key in resolved_cache:
                    cached_url = resolved_cache[ref_lookup_key]
                    if cached_url.startswith("http://") or cached_url.startswith("https://"):
                        if "nexusmods.com" in cached_url and "/mods/" in cached_url:
                            reference = parse_reference(cached_url, instance, getattr(reference, "game", None))
                        else:
                            direct_url = cached_url
                    else:
                        reference = parse_reference(cached_url, instance, getattr(reference, "game", None))

                download_result = download_reference(
                    instance,
                    reference,
                    api_key=nexus_api_key,
                    game=nexus.get("game"),
                    file_name=nexus.get("file") if isinstance(nexus.get("file"), str) and not str(nexus.get("file")).isdigit() else None,
                    output=spec.get("output"),
                    replace=bool(spec.get("replace", replace)),
                    expected_sha256=spec.get("sha256"),
                    auto_download=auto_download,
                    direct_url=direct_url,
                )
                archive = str(download_result["path"])
            else:
                raise Mo2Error("Manifest mod entry requires path, url, nxm, or nexus.")
            if "fomod" in spec and not isinstance(spec.get("fomod"), dict):
                raise Mo2Error("Manifest fomod field must be an object.")
            fomod = spec.get("fomod") or {}
            if not isinstance(fomod.get("select", {}), dict) or not isinstance(fomod.get("flags", {}), dict):
                raise Mo2Error("Manifest fomod.select and fomod.flags must be objects.")
            metadata_updates = {}
            if isinstance(download_result, dict) and download_result.get("source") == "nexus":
                metadata_updates = {
                    "modID": download_result.get("mod_id"),
                    "fileID": download_result.get("file_id"),
                    "repository": "Nexus",
                }
                if download_result.get("version"):
                    metadata_updates["version"] = download_result["version"]
                metadata_updates = {key: value for key, value in metadata_updates.items() if value is not None}
            installed = install_archive(instance, archive, selected_profile, name=spec.get("name"), disabled=bool(spec.get("disabled", False)), replace=bool(spec.get("replace", replace)), allow_fomod="fomod" in spec, source=spec.get("source_dir"), fomod_selections=fomod.get("select"), fomod_flags=fomod.get("flags"), fomod_game_version=fomod.get("game_version"), metadata_updates=metadata_updates, separator=spec.get("separator"), fomod_reuse="never")
            if download_result:
                installed["download"] = download_result
            installed["separator"] = spec.get("separator")
            result["mods"].append(installed)
            _remember_journal(installed, journal_ids)
        except (Mo2Error, OSError) as error:
            failure = {"name": spec.get("name"), "error": str(error)}
            errors.append(failure)
            if not continue_on_error:
                rollback_errors = _rollback(instance, journal_ids)
                detail = f" Rollback incomplete: {'; '.join(rollback_errors)}" if rollback_errors else ""
                raise Mo2Error(f"Manifest mod installation failed ({spec.get('name', '<unnamed>')}): {error}.{detail}") from error

    groups: dict[str, list[str]] = {}
    for spec in mod_specs:
        separator = spec.get("separator")
        name = spec.get("name")
        if isinstance(separator, str) and isinstance(name, str):
            groups.setdefault(separator, []).append(name)
    for separator_spec in separator_specs:
        if isinstance(separator_spec.get("name"), str) and isinstance(separator_spec.get("mods"), list):
            groups.setdefault(separator_spec["name"], []).extend(str(name) for name in separator_spec["mods"])
    existing_names = {entry.name for entry in instance.profile_files(selected_profile)[1].entries}
    successful_names = existing_names | {str(item.get("name")) for item in result["mods"] if isinstance(item, dict) and item.get("name")}
    for separator, names in groups.items():
        names = [name for name in names if name in successful_names]
        if names:
            try:
                grouped = group_separator(instance, selected_profile, separator, names)
            except (Mo2Error, OSError) as error:
                errors.append({"separator": separator, "error": str(error)})
                if continue_on_error:
                    continue
                rollback_errors = _rollback(instance, journal_ids)
                detail = f" Rollback incomplete: {'; '.join(rollback_errors)}" if rollback_errors else ""
                raise Mo2Error(f"Manifest grouping failed ({separator}): {error}.{detail}") from error
            result["groups"].append(grouped)
            _remember_journal(grouped, journal_ids)
    try:
        result["extras"] = _apply_extras(instance, root, separator_specs, selected_profile, journal_ids)
    except (Mo2Error, OSError) as error:
        errors.append({"extras": True, "error": str(error)})
        if not continue_on_error:
            rollback_errors = _rollback(instance, journal_ids)
            detail = f" Rollback incomplete: {'; '.join(rollback_errors)}" if rollback_errors else ""
            raise Mo2Error(f"Manifest extra sections failed: {error}.{detail}") from error
    result["errors"] = errors
    return result


# --- export and the sections it writes (order, separator colors, MO2 plugins, settings, Stock Game) -----

MANIFEST_VERSION = 2
DEFAULT_SETTINGS_SECTIONS = ("Settings", "PluginPersistance", "Plugins")
_VOLATILE_KEY = re.compile(r"geometry|splitter|windowstate|header|state$", re.IGNORECASE)


def _meta(values: dict[str, object], key: str) -> str:
    for name, value in values.items():
        if name.casefold() == key.casefold():
            return str(value).strip()
    return ""


def _positive(value: str) -> int | None:
    try:
        number = int(value)
    except ValueError:
        return None
    return number if number > 0 else None


def export(
    instance: Instance,
    output: str | Path,
    profile: str | None = None,
    with_paths: bool = False,
    settings_sections: list[str] | None = None,
) -> dict[str, object]:
    """Write a manifest that ``apply`` can read back: mods with their sources, separators with colors, order, MO2 plugins, settings, Stock Game version."""
    from . import mo2plugins, settings as ini_settings, stockgame
    from .metadata import ModMetadata
    from .separators import _display_name, get_color

    selected = instance.profile_name(profile)
    model = ModList.read(instance.profiles_dir / selected / "modlist.txt")
    entries = model.entries
    domain = game_domain(instance, None) if instance.game_name else None
    destination = Path(output).expanduser().resolve()

    # In modlist.txt the mods directly above a separator belong to it.
    separator_of: dict[str, str] = {}
    pending: list[str] = []
    for entry in entries:
        if entry.name.casefold().endswith("_separator"):
            for name in pending:
                separator_of[name] = _display_name(entry.name)
            pending = []
        else:
            pending.append(entry.name)

    separators: list[dict[str, object]] = []
    mods: list[dict[str, object]] = []
    for entry in entries:
        path = instance.mods_dir / entry.name
        if entry.name.casefold().endswith("_separator"):
            item: dict[str, object] = {"name": _display_name(entry.name)}
            color = get_color(path) if path.is_dir() else None
            if color:
                item["color"] = color
            separators.append(item)
            continue
        values = ModMetadata.read(path).values if path.is_dir() else {}
        spec: dict[str, object] = {"name": entry.name, "if_missing": True}
        if entry.name in separator_of:
            spec["separator"] = separator_of[entry.name]
        if not entry.enabled:
            spec["disabled"] = True
        if _meta(values, "version"):
            spec["version"] = _meta(values, "version")
        mod_id, file_id = _positive(_meta(values, "modid")), _positive(_meta(values, "fileid"))
        direct, manual = _meta(values, "directURL"), _meta(values, "manualURL")
        installation = _meta(values, "installationFile")
        archive = (instance.downloads_dir / Path(installation).name) if installation else None
        if with_paths and archive is not None and archive.is_file():
            spec["path"] = str(archive)
        if mod_id and file_id and domain:
            spec["nexus"] = {"game": domain, "mod_id": mod_id, "file_id": file_id}
        elif direct:
            spec["url"] = direct
        if manual:
            spec["manual_url"] = manual
        if installation:
            spec["installation_file"] = Path(installation).name
        if not any(key in spec for key in ("path", "nexus", "url")):
            spec["source_missing"] = True
        mods.append(spec)

    plugins: list[dict[str, object]] = []
    try:
        for name, value in sorted(mo2plugins.read_manifest(instance).items(), key=lambda item: item[0].casefold()):
            plugins.append({"name": name, "archive": value.get("archive")})
    except Mo2Error:
        pass

    settings: dict[str, dict[str, str]] = {}
    for section in settings_sections or list(DEFAULT_SETTINGS_SECTIONS):
        try:
            items = ini_settings.list_settings(instance, section, raw=True)
        except Mo2Error:
            continue
        kept = {str(item["key"]): str(item["value"]) for item in items if not _VOLATILE_KEY.search(str(item["key"]))}
        if kept:
            settings[section] = kept

    manifest: dict[str, object] = {
        "manifest_version": MANIFEST_VERSION,
        "profile": selected,
        "game": instance.game_name,
        "separators": separators,
        "mods": mods,
        "order": [{"name": entry.name, "enabled": entry.enabled} for entry in entries],
    }
    if plugins:
        manifest["mo2_plugins"] = plugins
    if settings:
        manifest["settings"] = settings
    stock = instance.base / "Stock Game"
    if stock.is_dir():
        try:
            version = stockgame.verify(stock)["version"]
        except Mo2Error:
            version = None
        if version:
            manifest["stock_game"] = {"expected_version": version}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {
        "output": str(destination),
        "profile": selected,
        "mods": len(mods),
        "separators": len(separators),
        "source_missing": [item["name"] for item in mods if item.get("source_missing")],
        "mo2_plugins": len(plugins),
        "settings_sections": sorted(settings),
        "stock_game": manifest.get("stock_game"),
    }


def _validate_extras(root: dict[str, Any]) -> None:
    order = root.get("order")
    if order is not None and (not isinstance(order, list) or any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in order)):
        raise Mo2Error("Manifest order must be a list of {name, enabled} objects.")
    plugins = root.get("mo2_plugins")
    if plugins is not None and (not isinstance(plugins, list) or any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in plugins)):
        raise Mo2Error("Manifest mo2_plugins must be a list of {name, archive} objects.")
    settings = root.get("settings")
    if settings is not None and (not isinstance(settings, dict) or any(not isinstance(value, dict) for value in settings.values())):
        raise Mo2Error("Manifest settings must be {section: {key: value}}.")
    stock = root.get("stock_game")
    if stock is not None and not isinstance(stock, dict):
        raise Mo2Error("Manifest stock_game must be an object.")


def _apply_extras(instance: Instance, root: dict[str, Any], separator_specs: list[dict[str, Any]], profile: str, journal_ids: list[str]) -> dict[str, object]:
    """Order/enabled state, separator colors, MO2 plugins, settings and the Stock Game check."""
    from . import mo2plugins, settings as ini_settings, stockgame
    from .journal import record
    from .separators import _find_separator_path, get_color, set_color

    extras: dict[str, object] = {}
    modlist_path = instance.profiles_dir / profile / "modlist.txt"
    profiles_backup: list[dict[str, str]] = []
    state_files: list[dict[str, object]] = []

    colors = [(str(spec["name"]), str(spec["color"])) for spec in separator_specs if isinstance(spec.get("name"), str) and spec.get("color")]
    recolored: list[str] = []
    for name, color in colors:
        path = _find_separator_path(instance, name)
        if path is None:
            continue
        if (get_color(path) or "").casefold() == color.casefold():
            continue
        meta = path / "meta.ini"
        state_files.append({"path": str(meta), "exists": meta.is_file(), "content": read_text_exact(meta) if meta.is_file() else ""})
        set_color(path, color)
        recolored.append(name)
    extras["separator_colors"] = recolored

    order = root.get("order")
    if isinstance(order, list) and order:
        profiles_backup.append({"path": str(modlist_path), "content": read_text_exact(modlist_path) if modlist_path.exists() else ""})
        model = ModList.read(modlist_path)
        rank = {str(item["name"]).casefold(): index for index, item in enumerate(order)}
        present = [entry for entry in model.entries if entry.name.casefold() in rank]
        queue = iter(sorted(present, key=lambda entry: rank[entry.name.casefold()]))
        model.lines = [next(queue) if not isinstance(line, str) and line.name.casefold() in rank else line for line in model.lines]
        for item in order:
            found = model.find(str(item["name"]))
            if found is not None and "enabled" in item and found.enabled != bool(item["enabled"]):
                model.set_enabled(found.name, bool(item["enabled"]))
        write_text(modlist_path, model.render())
        extras["order"] = {"applied": len(present), "missing": [str(item["name"]) for item in order if model.find(str(item["name"])) is None]}
    if profiles_backup or state_files:
        entry = record(instance, "manifest_order", profiles=profiles_backup, state_files=state_files)
        journal_ids.append(entry["id"])

    wanted = root.get("mo2_plugins") or []
    if wanted:
        outcomes = []
        installed = {item["name"].casefold() for item in mo2plugins.list_installed(instance)}
        for item in wanted:
            name, archive = str(item["name"]), item.get("archive")
            if name.casefold() in installed:
                outcomes.append({"name": name, "status": "present"})
                continue
            candidate = instance.downloads_dir / Path(str(archive)).name if archive else None
            if candidate is not None and candidate.is_file():
                installed_result = mo2plugins.install(instance, candidate, name)
                _remember_journal(installed_result, journal_ids)
                outcomes.append({"name": name, "status": "installed"})
            else:
                outcomes.append({"name": name, "status": "missing", "archive": archive})
        extras["mo2_plugins"] = outcomes

    settings = root.get("settings") or {}
    applied = []
    for section, values in settings.items():
        for key, value in values.items():
            current = None
            try:
                current = ini_settings.get_setting(instance, section, key, raw=True)["raw"]
            except Mo2Error:
                pass
            if current == str(value):
                continue
            changed = ini_settings.set_setting(instance, section, key, str(value), raw=True)
            _remember_journal(changed, journal_ids)
            applied.append(f"{section}/{key}")
    if settings:
        extras["settings_changed"] = applied

    stock = root.get("stock_game")
    if isinstance(stock, dict) and stock.get("expected_version"):
        directory = instance.base / "Stock Game"
        if directory.is_dir():
            try:
                verified = stockgame.verify(directory, str(stock["expected_version"]))
                extras["stock_game"] = {"expected_version": stock["expected_version"], "version": verified.get("version"), "ok": verified["ok"]}
            except Mo2Error as error:
                extras["stock_game"] = {"expected_version": stock["expected_version"], "ok": False, "error": str(error)}
        else:
            extras["stock_game"] = {"expected_version": stock["expected_version"], "ok": False, "error": "Stock Game folder not found"}
    return extras
