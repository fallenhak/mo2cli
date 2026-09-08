from __future__ import annotations

import re
import datetime as dt
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .formats import ModList, write_text

if TYPE_CHECKING:
    from .workspace import Instance


def _scalar(value: str):
    value = value.strip()
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        return value


class IniDocument:
    """Small loss-minimizing editor for QSettings-style INI files.

    QSettings allows root-level keys before any section, which configparser does not.
    Keeping the original lines also prevents metadata edits from rewriting unknown MO2
    keys or plugin-owned sections.
    """

    def __init__(self, lines: list[str] | None = None):
        self.lines = lines or []

    @classmethod
    def read(cls, path: Path) -> "IniDocument":
        if not path.exists():
            return cls([])
        return cls(path.read_text(encoding="utf-8-sig", errors="replace").splitlines())

    def _section_bounds(self, section: str | None) -> tuple[int, int]:
        if section is None:
            start = 0
            for index, line in enumerate(self.lines):
                if line.strip().startswith("[") and line.strip().endswith("]"):
                    return start, index
            return start, len(self.lines)
        wanted = section.casefold()
        start = None
        for index, line in enumerate(self.lines):
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                if start is not None:
                    return start, index
                if stripped[1:-1].strip().casefold() == wanted:
                    start = index + 1
        return (start, len(self.lines)) if start is not None else (-1, -1)

    def get(self, key: str, default=None, section: str | None = None):
        start, end = self._section_bounds(section)
        wanted = key.casefold()
        if start < 0:
            return default
        for line in self.lines[start:end]:
            match = re.match(r"^\s*([^=:#]+?)\s*[=:]\s*(.*?)\s*$", line)
            if match and match.group(1).strip().casefold() == wanted:
                return _scalar(match.group(2))
        return default

    def as_dict(self, section: str | None = None) -> dict[str, object]:
        start, end = self._section_bounds(section)
        result: dict[str, object] = {}
        if start < 0:
            return result
        for line in self.lines[start:end]:
            match = re.match(r"^\s*([^=:#]+?)\s*[=:]\s*(.*?)\s*$", line)
            if match:
                result[match.group(1).strip()] = _scalar(match.group(2))
        return result

    def set(self, key: str, value: object, section: str | None = None) -> None:
        rendered = str(value).lower() if isinstance(value, bool) else str(value)
        start, end = self._section_bounds(section)
        if start >= 0:
            pattern = re.compile(rf"^(\s*{re.escape(key)}\s*[=:]\s*).*$", re.IGNORECASE)
            for index in range(start, end):
                if pattern.match(self.lines[index]):
                    self.lines[index] = f"{key}={rendered}"
                    return
            self.lines.insert(end, f"{key}={rendered}")
            return
        if section is None:
            self.lines.append(f"{key}={rendered}")
            return
        if self.lines and self.lines[-1].strip():
            self.lines.append("")
        self.lines.extend([f"[{section}]", f"{key}={rendered}"])

    def remove(self, key: str, section: str | None = None) -> bool:
        start, end = self._section_bounds(section)
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*[=:].*$", re.IGNORECASE)
        for index in range(start, end):
            if pattern.match(self.lines[index]):
                del self.lines[index]
                return True
        return False

    def write(self, path: Path) -> None:
        content = "\r\n".join(self.lines)
        if self.lines:
            content += "\r\n"
        write_text(path, content)


@dataclass
class ModMetadata:
    mod_path: Path
    values: dict[str, object]

    @classmethod
    def read(cls, mod_path: Path) -> "ModMetadata":
        document = IniDocument.read(mod_path / "meta.ini")
        values = document.as_dict()
        values.update(document.as_dict("General"))
        return cls(mod_path, values)

    def to_dict(self) -> dict[str, object]:
        return dict(self.values)

    def get(self, key: str, default=None):
        return self.values.get(key, default)

    def update(self, updates: dict[str, object]) -> None:
        document = IniDocument.read(self.mod_path / "meta.ini")
        for key, value in updates.items():
            _remove_all(document, key)
            while document.remove(key, section="General"):
                pass
            if value is None:
                self.values.pop(key, None)
            else:
                document.set(key, value, section="General")
                self.values[key] = value
        document.write(self.mod_path / "meta.ini")


def _metadata_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).casefold())


def _remove_all(document: IniDocument, key: str) -> None:
    while document.remove(key):
        pass


def _game_domain(instance: "Instance") -> str:
    value = instance.game_name.casefold().strip()
    if value in {"skyrim special edition", "skyrim se", "skyrim special edition (steam)"}:
        return "skyrimspecialedition"
    return re.sub(r"[^a-z0-9]+", "", value)


def _stable_nexus_url(instance: "Instance", mod_id: object, file_id: object) -> str:
    return f"https://www.nexusmods.com/{_game_domain(instance)}/mods/{mod_id}?tab=files&file_id={file_id}"


def _sidecar_candidates(downloads: Path, mod_name: str, installation_file: str) -> list[Path]:
    sidecars = sorted(downloads.glob("*.meta"), key=lambda path: path.name.casefold()) if downloads.is_dir() else []
    if installation_file:
        exact = downloads / f"{installation_file}.meta"
        if exact.is_file():
            return [exact]
    wanted = _metadata_key(mod_name)
    matches = []
    for path in sidecars:
        values = _combined_values(IniDocument.read(path))
        for key in ("modName", "name"):
            if _metadata_key(values.get(key, "")) == wanted:
                matches.append(path)
                break
    if matches:
        return matches
    # These two MO2 folder names are commonly split from the same Nexus mod.
    if wanted == _metadata_key("Engine Fixes - SKSE64 Preloader"):
        return [path for path in sidecars if "preloader" in path.name.casefold()]
    if wanted == _metadata_key("EngineFixes"):
        return [path for path in sidecars if "main file" in path.name.casefold() and "engine fixes" in path.name.casefold()]
    return []


def _combined_values(document: IniDocument) -> dict[str, object]:
    values = document.as_dict()
    values.update(document.as_dict("General"))
    return values


def _set_general(document: IniDocument, key: str, value: object) -> None:
    _remove_all(document, key)
    while document.remove(key, section="General"):
        pass
    document.set(key, value, section="General")


def _normalize_name(name: str) -> str:
    if _metadata_key(name) == _metadata_key("Unofficial Skyrim Special Edition Patch"):
        return "Unofficial Skyrim Special Edition Patch - USSEP"
    return name


def _normalize_document(document: IniDocument, values: dict[str, object], mod_name: str) -> None:
    # MO2 reads mod metadata from [General]. Older CLI versions wrote these
    # keys at the INI root, which made the metadata appear empty in MO2.
    keys = {
        "installationFile", "gameName", "modID", "modid", "fileID", "fileid", "file_id",
        "version", "newestVersion", "repository", "url", "name", "modName",
        "category", "ignoredVersion", "comments", "notes", "nexusDescription",
        "hasCustomURL", "nexusFileStatus", "lastNexusQuery", "lastNexusUpdate",
        "nexusLastModified", "converted", "validated", "color", "endorsed", "tracked",
    }
    for key in keys:
        current = values.get(key)
        if current is not None and current != "":
            canonical = {"modid": "modID", "fileid": "fileID", "file_id": "fileID"}.get(key, key)
            _set_general(document, canonical, current)
    display_name = _normalize_name(mod_name)
    _set_general(document, "name", display_name)
    _set_general(document, "modName", display_name)
    mod_id = values.get("modID", values.get("modid"))
    file_id = values.get("fileID", values.get("fileid", values.get("file_id")))
    if mod_id not in (None, "", 0) and file_id not in (None, "", 0):
        while document.remove("size", section="installedFiles"):
            pass
        document.set("size", 1, section="installedFiles")
        while document.remove("1\\modid", section="installedFiles"):
            pass
        while document.remove("1\\fileid", section="installedFiles"):
            pass
        document.set("1\\modid", mod_id, section="installedFiles")
        document.set("1\\fileid", file_id, section="installedFiles")


def _repair_download_sidecar(
    instance: "Instance",
    mod_name: str,
    values: dict[str, object],
) -> str | None:
    """Repair the MO2 Downloads sidecar belonging to an installed archive.

    MO2 stores download metadata in ``<archive>.meta``.  A marker-only file
    (for example ``removed=false``) is not enough for the Downloads list to
    show the mod, version, and Nexus IDs.  Rebuild only when the archive is
    present and keep an existing direct download URL when one is available.
    """
    installation_file = str(values.get("installationFile", "") or "").strip()
    if not installation_file:
        return None
    archive = instance.downloads_dir / installation_file
    if not archive.is_file():
        return None

    sidecar = archive.with_name(archive.name + ".meta")
    document = IniDocument.read(sidecar)
    current = _combined_values(document)
    mod_id = values.get("modID", values.get("modid"))
    file_id = values.get("fileID", values.get("fileid", values.get("file_id")))
    repository = values.get("repository") or current.get("repository") or "Nexus"
    version = values.get("version") or current.get("version") or ""
    url = current.get("url") or values.get("url") or ""
    if str(repository).casefold() == "nexus" and mod_id not in (None, "", 0) and file_id not in (None, "", 0):
        # A previously stored CDN URL is useful for reinstalling; otherwise
        # retain the stable page URL from the installed mod metadata.
        url = url or _stable_nexus_url(instance, mod_id, file_id)

    updates = {
        "gameName": values.get("gameName") or current.get("gameName") or instance.game_name,
        "modID": mod_id if mod_id not in (None, "", 0) else current.get("modID", current.get("modid", 0)),
        "fileID": file_id if file_id not in (None, "", 0) else current.get("fileID", current.get("fileid", 0)),
        "url": url,
        "name": current.get("name") or installation_file,
        "modName": values.get("name") or values.get("modName") or mod_name,
        "version": version,
        "newestVersion": values.get("newestVersion") or current.get("newestVersion") or version,
        "repository": repository,
        "installed": True,
        "uninstalled": False,
        "paused": False,
        "removed": False,
    }
    for key, value in updates.items():
        if value not in (None, ""):
            _set_general(document, key, value)
    document.write(sidecar)
    if mod_id not in (None, "", 0):
        try:
            target_mid = int(mod_id)
            for other_sidecar in instance.downloads_dir.glob("*.meta"):
                if other_sidecar.name.casefold() == sidecar.name.casefold():
                    continue
                try:
                    other_doc = IniDocument.read(other_sidecar)
                    other_mid = other_doc.get("modID", section="General") or other_doc.get("modid", section="General")
                    if other_mid and int(other_mid) == target_mid:
                        _set_general(other_doc, "installed", True)
                        _set_general(other_doc, "uninstalled", False)
                        other_doc.write(other_sidecar)
                except Exception:
                    pass
        except (ValueError, TypeError):
            pass
    return str(sidecar)


def sync_metadata(instance: "Instance", profile: str | None = None, names: list[str] | None = None, include_unlisted: bool = False) -> dict[str, object]:
    """Backfill MO2/Wabbajack metadata from MO2 download sidecars.

    Nexus downloads carry the authoritative mod/file IDs in ``*.meta`` files.
    This keeps locally installed mods usable by Wabbajack even when the archive
    was installed after a separate download step.
    """
    profile_name = instance.profile_name(profile)
    model = ModList.read(instance.profiles_dir / profile_name / "modlist.txt")
    wanted = {_metadata_key(name) for name in names or []}
    entries = []
    listed = set()
    for entry in model.entries:
        listed.add(entry.name.casefold())
        if entry.foreign or entry.name.casefold().endswith("_separator"):
            continue
        if not entry.enabled and not include_unlisted:
            continue
        if wanted and _metadata_key(entry.name) not in wanted:
            continue
        entries.append(entry.name)
    if include_unlisted and instance.mods_dir.is_dir():
        for path in instance.mods_dir.iterdir():
            if path.is_dir() and path.name.casefold() not in listed and not path.name.casefold().endswith("_separator"):
                if not wanted or _metadata_key(path.name) in wanted:
                    entries.append(path.name)

    updated = []
    normalized = []
    skipped = []
    ambiguous = []
    backups = []
    download_sidecars = []
    backup_root = instance.base / ".mo2cli-backups" / f"metadata-sync-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    for name in entries:
        mod_path = instance.mods_dir / name
        meta_path = mod_path / "meta.ini"
        if not meta_path.is_file():
            skipped.append({"name": name, "reason": "meta.ini missing"})
            continue
        document = IniDocument.read(meta_path)
        values = _combined_values(document)
        installation_file = str(values.get("installationFile", "") or "")
        candidates = _sidecar_candidates(instance.downloads_dir, name, installation_file)
        if len(candidates) > 1:
            item = {"name": name, "reason": "download metadata not found" if not candidates else "multiple download metadata files found"}
            (ambiguous if candidates else skipped).append(item)
            continue
        sidecar = candidates[0] if candidates else None
        source = _combined_values(IniDocument.read(sidecar)) if sidecar else {}
        mod_id = source.get("modID", source.get("modid", values.get("modID", values.get("modid"))))
        file_id = source.get("fileID", source.get("fileid", values.get("fileID", values.get("fileid", values.get("file_id")))))
        version = source.get("version", values.get("version", ""))
        repository = source.get("repository", values.get("repository", ""))
        if sidecar and (mod_id in (None, "", 0) or file_id in (None, "", 0)):
            # A plain ``removed=false`` marker can exist for a manually
            # downloaded non-Nexus archive. Keep the metadata already in the
            # mod's meta.ini instead of treating that marker as authoritative.
            if values.get("repository") and values.get("url") and values.get("version"):
                sidecar = None
            else:
                skipped.append({"name": name, "reason": "sidecar missing modID/fileID"})
                continue
        if not sidecar and not all(values.get(key) not in (None, "", 0) for key in ("installationFile", "version", "repository", "url")):
            skipped.append({"name": name, "reason": "download metadata not found"})
            continue
        if sidecar:
            values.update({"modID": mod_id, "fileID": file_id, "version": version, "repository": repository})
            if str(repository).casefold() == "nexus":
                values["url"] = _stable_nexus_url(instance, mod_id, file_id)
            if not installation_file:
                values["installationFile"] = sidecar.name[:-5]
        backup_path = backup_root / name / "meta.ini"
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(meta_path, backup_path)
        backups.append(str(backup_path))
        if version not in (None, ""):
            values["newestVersion"] = source.get("newestVersion") or values.get("newestVersion") or version
        _normalize_document(document, values, name)
        document.write(meta_path)
        repaired_sidecar = _repair_download_sidecar(instance, name, values)
        if repaired_sidecar:
            download_sidecars.append(repaired_sidecar)
        if sidecar:
            updated.append({"name": name, "modID": mod_id, "fileID": file_id, "source": sidecar.name})
        else:
            normalized.append(name)
    if not backups and backup_root.exists():
        shutil.rmtree(backup_root)
    return {"profile": profile_name, "updated": updated, "normalized": normalized, "skipped": skipped, "ambiguous": ambiguous, "downloadSidecars": download_sidecars, "backup": str(backup_root) if backups else None}
