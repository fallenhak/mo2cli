from __future__ import annotations

import os
import re
import shutil
import urllib.parse
import urllib.request
from pathlib import Path

from .archives import sha256
from .metadata import IniDocument, ModMetadata
from .workspace import Instance, Mo2Error

# Nexus domain names -> the short names MO2 (and therefore Wabbajack's
# GetByMO2ArchiveName) uses in ``gameName``.
MO2_GAME_NAMES = {
    "skyrimspecialedition": "SkyrimSE",
    "skyrim": "Skyrim",
    "skyrimvr": "SkyrimVR",
    "fallout4": "Fallout4",
    "fallout4vr": "Fallout4VR",
    "newvegas": "FalloutNV",
    "fallout3": "Fallout3",
    "oblivion": "Oblivion",
    "morrowind": "Morrowind",
    "starfield": "Starfield",
}


def mo2_game_name(value: str) -> str:
    return MO2_GAME_NAMES.get(value.strip().casefold(), value.strip())


def _http_url(value: str, label: str) -> str:
    value = str(value).strip()
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise Mo2Error(f"{label} must be an absolute http(s) URL: {value!r}")
    if any(char in value for char in "\r\n"):
        raise Mo2Error(f"{label} cannot contain line breaks.")
    return value


def _positive_int(value: object, label: str) -> int:
    try:
        number = int(str(value).strip())
    except ValueError:
        raise Mo2Error(f"{label} must be a positive integer: {value!r}") from None
    if number <= 0:
        raise Mo2Error(f"{label} must be a positive integer: {value!r}")
    return number


def format_headers(headers: dict[str, str] | list[str] | None) -> str:
    """Render request headers the way Wabbajack's ``directURLHeaders`` expects.

    Each header is ``Name: value`` and headers are separated by ``|``.
    """
    if not headers:
        return ""
    items = [f"{key}: {value}" for key, value in headers.items()] if isinstance(headers, dict) else list(headers)
    rendered = []
    for item in items:
        name, separator, value = str(item).partition(":")
        if not separator or not name.strip():
            raise Mo2Error(f"Header must look like 'Name: value': {item!r}")
        if "|" in item or "\r" in item or "\n" in item:
            raise Mo2Error(f"Header cannot contain '|' or line breaks: {item!r}")
        rendered.append(f"{name.strip()}: {value.strip()}")
    return "|".join(rendered)


def write_meta(
    meta_path: Path,
    *,
    direct_url: str | None = None,
    direct_headers: dict[str, str] | list[str] | None = None,
    manual_url: str | None = None,
    prompt: str | None = None,
    game_name: str | None = None,
    mod_id: int | str | None = None,
    file_id: int | str | None = None,
    installed: bool | None = None,
    uninstalled: bool | None = None,
    name: str | None = None,
    mod_name: str | None = None,
    version: str | None = None,
    repository: str | None = None,
    url: str | None = None,
    defaults: dict[str, object] | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    """Create or update one ``<archive>.meta`` sidecar.

    This is the single place that writes Wabbajack source keys, so every code
    path (``downloads fetch``, ``mods install <URL>``, manifests, Nexus
    downloads, ``downloads meta``) produces the same syntax. Keys exactly as
    Wabbajack reads them are ``directURL``, ``directURLHeaders`` (``|``
    separated), ``manualURL``, ``prompt``, ``gameName``, ``modID`` and
    ``fileID``; MO2's own keys (``installed``, ``uninstalled``, ``removed``,
    ``name``, ``modName``, ``version``, ``repository``, ``url``) share the
    ``[General]`` section. Arguments left as ``None`` are not touched and unknown
    existing keys are preserved. ``defaults`` are written only when the key is
    still absent. Returns the resulting ``[General]`` values.
    """
    existing = IniDocument.read(meta_path)
    updates: dict[str, object] = {}
    if direct_url is not None:
        updates["directURL"] = _http_url(direct_url, "directURL")
    if direct_headers:
        if direct_url is None and existing.get("directURL", section="General") is None:
            raise Mo2Error("directURLHeaders only make sense together with a directURL.")
        updates["directURLHeaders"] = format_headers(direct_headers)
    if manual_url is not None:
        updates["manualURL"] = _http_url(manual_url, "manualURL")
    if prompt is not None:
        if manual_url is None and existing.get("manualURL", section="General") is None:
            raise Mo2Error("prompt only makes sense together with a manualURL.")
        if "\r" in prompt or "\n" in prompt:
            raise Mo2Error("prompt cannot contain line breaks.")
        updates["prompt"] = prompt
    if (mod_id is None) != (file_id is None):
        raise Mo2Error("Nexus sources need both modID and fileID.")
    if mod_id is not None:
        updates["modID"] = _positive_int(mod_id, "modID")
        updates["fileID"] = _positive_int(file_id, "fileID")
        if not game_name:
            raise Mo2Error("Nexus sources need a gameName (for example SkyrimSE).")
    if game_name:
        updates["gameName"] = game_name
    if installed is not None:
        updates["installed"] = installed
        updates["uninstalled"] = (not installed) if uninstalled is None else uninstalled
    elif uninstalled is not None:
        updates["uninstalled"] = uninstalled
    for key, value in (("name", name), ("modName", mod_name), ("version", version), ("repository", repository), ("url", url)):
        if value is not None:
            updates[key] = value
    document = existing
    for key, value in updates.items():
        document.set(key, value, section="General")
    for key, value in (defaults or {}).items():
        if document.get(key, section="General") is None:
            document.set(key, value, section="General")
    if not any(line.strip().casefold() == "[general]" for line in document.lines):
        document.lines.insert(0, "[General]")
    if not dry_run:
        document.write(meta_path)
    return document.as_dict("General")


def wabbajack_source(values: dict[str, object]) -> str | None:
    """Name the source Wabbajack would pick from ``[General]`` values, if any."""

    def positive(key: str) -> bool:
        try:
            return int(str(values.get(key, "")).strip()) > 0
        except ValueError:
            return False

    if values.get("gameName") and positive("modID") and positive("fileID"):
        return "nexus"
    if str(values.get("directURL") or "").strip():
        return "directURL"
    if str(values.get("manualURL") or "").strip():
        return "manualURL"
    return None


_SHORT_GAME_NAMES = {
    "skyrim special edition": "SkyrimSE",
    "skyrim special edition (steam)": "SkyrimSE",
    "skyrim": "Skyrim",
    "skyrim vr": "SkyrimVR",
    "fallout 4": "Fallout4",
    "fallout new vegas": "FalloutNV",
    "starfield": "Starfield",
}


def default_game_name(instance: Instance) -> str:
    """MO2's short ``gameName`` for the instance, falling back to SkyrimSE."""
    return _SHORT_GAME_NAMES.get(instance.game_name.casefold(), "SkyrimSE" if not instance.game_name else instance.game_name)


def parse_nexus_ids(value: str) -> tuple[int, int]:
    mod, separator, file = str(value).strip().partition(":")
    if not separator:
        raise Mo2Error(f"--nexus must be MODID:FILEID, got {value!r}")
    return _positive_int(mod, "Nexus modID"), _positive_int(file, "Nexus fileID")


def find_download(instance: Instance, archive: str | Path) -> Path:
    """Resolve an archive given as a path or as a file name in the downloads folder."""
    candidate = Path(archive).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    if candidate.name.endswith(".meta"):
        candidate = candidate.with_name(candidate.name[:-5])
    if instance.downloads_dir.is_dir():
        wanted = candidate.name.casefold()
        found = next((item for item in instance.downloads_dir.iterdir() if item.is_file() and item.name.casefold() == wanted), None)
        if found is not None:
            return found
    raise Mo2Error(f"Download archive not found: {archive}")


def set_meta(
    instance: Instance,
    archive: str | Path,
    direct_url: str | None = None,
    headers: list[str] | None = None,
    manual_url: str | None = None,
    prompt: str | None = None,
    nexus: str | None = None,
    game_name: str | None = None,
    installed: bool | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    """Create or update ``<archive>.meta`` for a download (``downloads meta``)."""
    sources = [bool(direct_url), bool(manual_url), bool(nexus)]
    if sum(sources) > 1:
        raise Mo2Error("Choose only one source: --direct-url, --manual-url or --nexus.")
    if not any(sources) and installed is None:
        raise Mo2Error("Nothing to write: give --direct-url, --manual-url, --nexus, or --installed/--not-installed.")
    if headers and not direct_url:
        raise Mo2Error("--header requires --direct-url.")
    if prompt and not manual_url:
        raise Mo2Error("--prompt requires --manual-url.")
    mod_id = file_id = None
    if nexus:
        mod_id, file_id = parse_nexus_ids(nexus)
    elif game_name:
        raise Mo2Error("--game-name only applies to --nexus.")
    path = find_download(instance, archive)
    meta = path.with_name(path.name + ".meta")
    is_new = not meta.is_file()
    values = write_meta(
        meta,
        direct_url=direct_url,
        direct_headers=headers,
        manual_url=manual_url,
        prompt=prompt,
        game_name=(game_name or default_game_name(instance)) if nexus else None,
        mod_id=mod_id,
        file_id=file_id,
        repository="Nexus" if nexus else None,
        installed=installed,
        name=path.name if is_new else None,
        defaults={"installed": False, "uninstalled": False, "removed": False} if is_new else None,
        dry_run=dry_run,
    )
    result: dict[str, object] = {"archive": path.name, "meta": str(meta), "created": is_new, "source": wabbajack_source(values), "values": values}
    if dry_run:
        result["dry_run"] = True
    return result


def audit_downloads(instance: Instance) -> dict[str, object]:
    """Report what would stop Wabbajack from re-downloading every archive.

    * archives without a ``.meta``;
    * archives whose ``.meta`` has no source key Wabbajack understands;
    * orphan ``.meta`` files (no archive next to them);
    * installed mods whose ``installationFile`` is not in the downloads folder.
    """
    downloads = instance.downloads_dir
    archives: dict[str, Path] = {}
    metas: dict[str, Path] = {}
    if downloads.is_dir():
        for item in sorted(downloads.iterdir(), key=lambda entry: entry.name.casefold()):
            if not item.is_file() or item.name.endswith(".part"):
                continue
            if item.name.casefold().endswith(".meta"):
                metas[item.name[:-5].casefold()] = item
            else:
                archives[item.name.casefold()] = item
    missing_meta: list[str] = []
    missing_source: list[dict[str, object]] = []
    sources: dict[str, int] = {}
    for key, archive in archives.items():
        meta = metas.get(key)
        if meta is None:
            missing_meta.append(archive.name)
            continue
        values = IniDocument.read(meta).as_dict("General")
        source = wabbajack_source(values)
        if source is None:
            missing_source.append({"archive": archive.name, "unknownArchive": str(values.get("unknownArchive", "")).casefold() == "true"})
        else:
            sources[source] = sources.get(source, 0) + 1
    orphan_meta = sorted((meta.name for key, meta in metas.items() if key not in archives), key=str.casefold)
    missing_archive: list[dict[str, str]] = []
    no_installation_file: list[str] = []
    if instance.mods_dir.is_dir():
        for mod in sorted(instance.mods_dir.iterdir(), key=lambda entry: entry.name.casefold()):
            if not mod.is_dir() or mod.name.casefold().endswith("_separator"):
                continue
            values = ModMetadata.read(mod).values
            installation_file = str(values.get("installationFile") or "").strip()
            if not installation_file:
                no_installation_file.append(mod.name)
            elif Path(installation_file).name.casefold() not in archives:
                missing_archive.append({"mod": mod.name, "installationFile": installation_file})
    problems = len(missing_meta) + len(missing_source) + len(missing_archive)
    return {
        "ok": problems == 0,
        "archives": len(archives),
        "sources": sources,
        "archives_without_meta": missing_meta,
        "archives_without_wabbajack_source": missing_source,
        "orphan_meta": orphan_meta,
        "mods_missing_archive": missing_archive,
        "mods_without_installation_file": no_installation_file,
    }


def list_downloads(instance: Instance, with_hash: bool = False) -> list[dict[str, object]]:
    if not instance.downloads_dir.exists():
        return []
    result = []
    for path in sorted((item for item in instance.downloads_dir.iterdir() if item.is_file()), key=lambda item: item.name.casefold()):
        item = {"name": path.name, "path": str(path), "bytes": path.stat().st_size}
        if with_hash:
            item["sha256"] = sha256(path)
        result.append(item)
    return result


def fetch(instance: Instance, url: str, output: str | None = None, replace: bool = False, headers: dict[str, str] | None = None, expected_sha256: str | None = None, record_source: bool = True) -> dict[str, object]:
    """Download ``url`` into the downloads folder and write its ``.meta``.

    ``record_source`` also stores ``directURL`` (and ``directURLHeaders``) so
    Wabbajack can re-download the archive. Callers that fetch short-lived signed
    links (Nexus CDN) pass ``False`` and write their own source keys.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise Mo2Error("Only HTTP(S) downloads are supported.")
    request_url = urllib.parse.urlunparse(
        parsed._replace(path=urllib.parse.quote(urllib.parse.unquote(parsed.path), safe="/:@!$&'()*+,;=-._~"))
    )
    name = output or Path(urllib.parse.unquote(parsed.path)).name
    if not name:
        raise Mo2Error("Could not determine filename from URL; specify --output.")
    target = instance.downloads_dir / Path(name).name
    if target.exists() and not replace:
        raise Mo2Error(f"Download already exists: {target}; use --replace to overwrite.")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Per-process name: fetch runs without the instance lock, so parallel downloads must not share a .part.
    temporary = target.with_name(f"{target.name}.{os.getpid()}.part")
    try:
        request = urllib.request.Request(request_url, headers=headers or {}, method="GET")
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as destination:
            shutil.copyfileobj(response, destination)
        digest = sha256(temporary)
        if expected_sha256 and digest.casefold() != expected_sha256.casefold():
            raise Mo2Error(f"SHA-256 verification failed: expected {expected_sha256}, got {digest}")
        temporary.replace(target)
        meta_target = target.with_name(target.name + ".meta")
        write_meta(
            meta_target,
            url=url,
            name=target.name,
            direct_url=request_url if record_source else None,
            direct_headers=headers if record_source and headers else None,
            defaults={"installed": False, "uninstalled": True, "removed": False},
        )
    finally:
        if temporary.exists():
            temporary.unlink()
    result = {"url": url, "path": str(target), "bytes": target.stat().st_size, "sha256": sha256(target)}
    if record_source:
        result["direct_url"] = request_url
    return result


def mark_installed(instance: Instance, archives: list[str | Path], installed: bool = True) -> list[dict[str, object]]:
    """Mark one or more download archives as installed or uninstalled."""
    if not instance.downloads_dir.exists():
        raise Mo2Error(f"Downloads directory not found: {instance.downloads_dir}")
    results = []
    for item in archives:
        name = Path(item).name
        target_meta = instance.downloads_dir / f"{name}.meta"
        target_archive = instance.downloads_dir / name
        if not target_meta.is_file() and not target_archive.is_file():
            found = next((f for f in instance.downloads_dir.iterdir() if f.name.casefold() in {name.casefold(), f"{name.casefold()}.meta"}), None)
            if found:
                if found.name.endswith(".meta"):
                    target_meta = found
                    target_archive = found.with_name(found.name[:-5])
                else:
                    target_archive = found
                    target_meta = found.with_name(found.name + ".meta")
            else:
                raise Mo2Error(f"Download archive not found: {name}")

        doc = IniDocument.read(target_meta) if target_meta.is_file() else IniDocument()
        doc.set("installed", installed, section="General")
        doc.set("uninstalled", not installed, section="General")
        if not doc.get("name", section="General"):
            doc.set("name", target_archive.name, section="General")
        doc.write(target_meta)
        results.append({"archive": target_archive.name, "installed": installed})
    return results


def sync_downloads(instance: Instance) -> dict[str, object]:
    """Synchronize download archive .meta states with installed mods, plugins, and tools."""
    from . import mo2plugins  # imported here: mo2plugins itself builds on write_meta

    if not instance.downloads_dir.exists():
        return {"updated": [], "already_installed": 0, "unmatched": []}

    installed_files: set[str] = set()
    installed_mod_ids: dict[int, list[str]] = {}
    installed_mod_names: set[str] = set()

    if instance.mods_dir.is_dir():
        for mod_folder in instance.mods_dir.iterdir():
            if not mod_folder.is_dir() or mod_folder.name.casefold().endswith("_separator"):
                continue
            installed_mod_names.add(mod_folder.name.casefold())
            meta_ini = mod_folder / "meta.ini"
            if meta_ini.is_file():
                try:
                    meta_doc = IniDocument.read(meta_ini)
                    mod_id = meta_doc.get("modID", section="General") or meta_doc.get("modid", section="General")
                    inst_file = meta_doc.get("installationFile", section="General") or meta_doc.get("installationFile")
                    if inst_file:
                        inst_str = str(inst_file).strip()
                        installed_files.add(inst_str.casefold())
                        if not mod_id and instance.downloads_dir.is_dir():
                            inst_meta = instance.downloads_dir / f"{inst_str}.meta"
                            if inst_meta.is_file():
                                try:
                                    inst_sdoc = IniDocument.read(inst_meta)
                                    mod_id = inst_sdoc.get("modID", section="General") or inst_sdoc.get("modid", section="General")
                                except Exception:
                                    pass
                    if mod_id:
                        try:
                            mid = int(mod_id)
                            if mid > 0:
                                installed_mod_ids.setdefault(mid, []).append(mod_folder.name)
                        except (ValueError, TypeError):
                            pass
                    custom_name = meta_doc.get("name", section="General") or meta_doc.get("modName", section="General")
                    if custom_name:
                        installed_mod_names.add(str(custom_name).strip().casefold())
                except Exception:
                    pass

    has_rootbuilder = (instance.base / "plugins" / "rootbuilder").is_dir() or any(
        p.name.casefold() == "rootbuilder.py" for p in (instance.base / "plugins").glob("*")
    ) if (instance.base / "plugins").is_dir() else False
    has_fomod_plus = mo2plugins.has_plugin_markers(instance, "fomod plus", "fomodplus", "fomod_plus")
    managed_plugin_archives = mo2plugins.installed_archive_names(instance)

    has_mo2 = (instance.base / "ModOrganizer.exe").is_file()

    tool_names = set()
    try:
        for tool in instance.executables():
            title = str(tool.get("title") or "").strip().casefold()
            binary = Path(str(tool.get("binary") or "")).stem.casefold()
            if title:
                tool_names.add(title)
            if binary:
                tool_names.add(binary)
    except Exception:
        pass

    updated = []
    already_installed = 0
    unmatched = []

    for item in sorted(instance.downloads_dir.iterdir(), key=lambda x: x.name.casefold()):
        if not item.is_file() or item.name.endswith(".meta") or item.name.endswith(".part"):
            continue

        meta_path = item.with_name(item.name + ".meta")
        doc = IniDocument.read(meta_path) if meta_path.is_file() else IniDocument()

        is_installed = doc.get("installed", section="General")
        if is_installed is True or str(is_installed).strip().lower() == "true":
            already_installed += 1
            continue

        match_reason = None
        archive_name_lower = item.name.casefold()

        if archive_name_lower in installed_files:
            match_reason = "matched installation file of installed mod"

        if not match_reason:
            meta_mod_id = doc.get("modID", section="General") or doc.get("modid", section="General") or doc.get("modID") or doc.get("modid")
            if meta_mod_id:
                try:
                    mid = int(meta_mod_id)
                    if mid > 0 and mid in installed_mod_ids:
                        matching_mods = ", ".join(installed_mod_ids[mid])
                        match_reason = f"matched modID {mid} ({matching_mods})"
                except (ValueError, TypeError):
                    pass

        if not match_reason and archive_name_lower in managed_plugin_archives:
            match_reason = f"matched MO2 plugin installed by mo2cli ({managed_plugin_archives[archive_name_lower]})"

        if not match_reason and has_fomod_plus and re.search(r"fomod[ ._-]*plus", archive_name_lower):
            match_reason = "matched installed MO2 plugin FOMOD Plus"

        if not match_reason and has_rootbuilder and ("root builder" in archive_name_lower or "rootbuilder" in archive_name_lower):
            match_reason = "matched installed MO2 plugin Root Builder"

        if not match_reason and has_mo2 and ("mod organizer" in archive_name_lower or "mod.organizer" in archive_name_lower):
            match_reason = "matched installed Mod Organizer 2"

        if not match_reason:
            for t in tool_names:
                if t and t in archive_name_lower:
                    match_reason = f"matched installed tool '{t}'"
                    break

        if not match_reason:
            doc_mod_name = str(doc.get("modName", section="General") or doc.get("name", section="General") or "").strip().casefold()
            for mname in installed_mod_names:
                if mname and (mname == doc_mod_name or mname in archive_name_lower):
                    match_reason = f"matched installed mod name '{mname}'"
                    break

        if match_reason:
            doc.set("installed", True, section="General")
            doc.set("uninstalled", False, section="General")
            if not doc.get("name", section="General"):
                doc.set("name", item.name, section="General")
            doc.write(meta_path)
            updated.append({"archive": item.name, "reason": match_reason})
        else:
            unmatched.append(item.name)

    return {
        "updated": updated,
        "already_installed": already_installed,
        "unmatched": unmatched,
    }
