from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from .downloads import fetch
from .installer import install_archive
from .nexus import NexusReference, download_reference, game_domain, parse_reference
from .separators import ensure as ensure_separator, group as group_separator
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
    mod_specs = _list(root.get("mods", []), "mods")
    separator_specs = _list(root.get("separators", []), "separators")
    result: dict[str, object] = {"manifest": str(source), "profile": selected_profile, "dry_run": dry_run, "separators": [], "mods": [], "groups": []}
    if dry_run:
        dry_separators = [{"name": item.get("name"), "before": item.get("before"), "after": item.get("after")} for item in separator_specs]
        declared = {str(item.get("name")).casefold() for item in separator_specs if isinstance(item.get("name"), str)}
        for item in mod_specs:
            name = item.get("separator")
            if isinstance(name, str) and name.casefold() not in declared:
                dry_separators.append({"name": name, "before": None, "after": None, "implicit": True})
                declared.add(name.casefold())
        result["separators"] = dry_separators
        result["mods"] = [{"name": item.get("name"), "source": item.get("source") or item.get("url") or item.get("nxm") or item.get("nexus") or item.get("path"), "separator": item.get("separator")} for item in mod_specs]
        result["groups"] = [{"separator": item.get("name"), "mods": item.get("mods", [])} for item in separator_specs if item.get("mods")]
        return result

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
    for spec in separator_specs:
        name = spec.get("name")
        if not isinstance(name, str) or not name.strip():
            raise Mo2Error("Manifest separator requires a name.")
        created = ensure_separator(instance, selected_profile, name, spec.get("before"), spec.get("after"))
        result["separators"].append(created)
    for name in sorted(referenced_separators - declared_separators):
        original = next((item.get("separator") for item in mod_specs if isinstance(item.get("separator"), str) and str(item.get("separator")).casefold() == name), None)
        if isinstance(original, str):
            result["separators"].append(ensure_separator(instance, selected_profile, original))

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

                ref_lookup_key = str(reference) if isinstance(reference, str) else f"https://www.nexusmods.com/{reference.game}/mods/{reference.mod_id}?tab=files&file_id={reference.file_id}"
                if ref_lookup_key in resolved_cache:
                    cached_url = resolved_cache[ref_lookup_key]
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
            installed = install_archive(instance, archive, selected_profile, name=spec.get("name"), disabled=bool(spec.get("disabled", False)), replace=bool(spec.get("replace", replace)), allow_fomod=bool(fomod), source=spec.get("source_dir"), fomod_selections=fomod.get("select"), fomod_flags=fomod.get("flags"), fomod_game_version=fomod.get("game_version"), metadata_updates=metadata_updates, separator=spec.get("separator"), fomod_reuse="never")
            if download_result:
                installed["download"] = download_result
            installed["separator"] = spec.get("separator")
            result["mods"].append(installed)
        except (Mo2Error, OSError) as error:
            failure = {"name": spec.get("name"), "error": str(error)}
            errors.append(failure)
            if not continue_on_error:
                raise Mo2Error(f"Manifest mod installation failed ({spec.get('name', '<unnamed>')}): {error}") from error

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
            result["groups"].append(group_separator(instance, selected_profile, separator, names))
    result["errors"] = errors
    return result
