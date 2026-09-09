from __future__ import annotations

import shutil
import urllib.parse
import urllib.request
from pathlib import Path

from .archives import sha256
from .metadata import IniDocument
from .workspace import Instance, Mo2Error


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


def fetch(instance: Instance, url: str, output: str | None = None, replace: bool = False, headers: dict[str, str] | None = None, expected_sha256: str | None = None) -> dict[str, object]:
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
    temporary = target.with_suffix(target.suffix + ".part")
    try:
        request = urllib.request.Request(request_url, headers=headers or {}, method="GET")
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as destination:
            shutil.copyfileobj(response, destination)
        digest = sha256(temporary)
        if expected_sha256 and digest.casefold() != expected_sha256.casefold():
            raise Mo2Error(f"SHA-256 verification failed: expected {expected_sha256}, got {digest}")
        temporary.replace(target)
        meta_target = target.with_name(target.name + ".meta")
        meta_doc = IniDocument.read(meta_target) if meta_target.is_file() else IniDocument()
        meta_doc.set("url", url, section="General")
        meta_doc.set("name", target.name, section="General")
        if meta_doc.get("installed", section="General") is None:
            meta_doc.set("installed", False, section="General")
        if meta_doc.get("uninstalled", section="General") is None:
            meta_doc.set("uninstalled", True, section="General")
        meta_doc.write(meta_target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {"url": url, "path": str(target), "bytes": target.stat().st_size, "sha256": sha256(target)}


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
