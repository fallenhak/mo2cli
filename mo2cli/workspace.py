from __future__ import annotations

import configparser
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .formats import ModList, PluginList, write_text
from .metadata import IniDocument, ModMetadata


class Mo2Error(RuntimeError):
    pass


def _qt_value(value: str) -> str:
    value = value.strip()
    if value.startswith("@ByteArray(") and value.endswith(")"):
        return value[len("@ByteArray(") : -1]
    return value


def _resolve(value: str, base: Path, root: Path) -> Path:
    value = _qt_value(value).replace("%BASE_DIR%", str(base)).strip().strip('"')
    path = Path(value)
    return path if path.is_absolute() else root / path


def _safe_name(name: str) -> None:
    if not name.strip() or name in {".", ".."} or re.search(r"[<>:\"/\\|?*]", name):
        raise Mo2Error(f"Invalid name: {name!r}")


def _safe_relative_path(value: str) -> tuple[str, ...]:
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise Mo2Error(f"File path escapes instance directory: {value!r}")
    return path.parts


@dataclass
class Instance:
    root: Path
    ini: Path
    base: Path
    mods_dir: Path
    profiles_dir: Path
    downloads_dir: Path
    overwrite_dir: Path
    game_name: str
    game_path: Path | None
    selected_profile: str | None

    @classmethod
    def open(cls, path: str | Path | None = None) -> "Instance":
        candidate = Path(path or ".").expanduser().resolve()
        if candidate.is_file():
            candidate = candidate.parent
        ini = candidate / "ModOrganizer.ini"
        if not ini.exists():
            ini = next((p for p in candidate.glob("*.ini") if p.name.casefold() == "modorganizer.ini"), ini)
        if not ini.exists() and not (candidate / "profiles").exists() and not (candidate / "mods").exists():
            raise Mo2Error(f"MO2 instance not found: {candidate}")

        parser = configparser.ConfigParser(interpolation=None)
        parser.read(ini, encoding="utf-8-sig") if ini.exists() else None
        settings = parser["Settings"] if parser.has_section("Settings") else {}
        base = _resolve(settings.get("base_directory", str(candidate)), candidate, candidate)
        mods = _resolve(settings.get("mod_directory", "%BASE_DIR%/mods"), base, candidate)
        profiles = _resolve(settings.get("profiles_directory", "%BASE_DIR%/profiles"), base, candidate)
        downloads = _resolve(settings.get("download_directory", "%BASE_DIR%/downloads"), base, candidate)
        overwrite = _resolve(settings.get("overwrite_directory", "%BASE_DIR%/overwrite"), base, candidate)
        general = parser["General"] if parser.has_section("General") else {}
        selected = _qt_value(general.get("selected_profile", "")) or None
        game_name = _qt_value(general.get("gamename") or general.get("game_name") or "")
        raw_game_path = general.get("gamepath") or general.get("game_path")
        game_path = _resolve(raw_game_path, base, candidate) if raw_game_path else None
        return cls(candidate, ini, base, mods, profiles, downloads, overwrite, game_name, game_path, selected)

    def profile_name(self, requested: str | None = None) -> str:
        name = requested or self.selected_profile
        if not name:
            names = self.list_profiles()
            if len(names) == 1:
                return names[0]
            raise Mo2Error("No profile specified and no selected profile found; use --profile.")
        _safe_name(name)
        return name

    def profile_path(self, requested: str | None = None) -> Path:
        path = self.profiles_dir / self.profile_name(requested)
        if not path.is_dir():
            raise Mo2Error(f"Profile not found: {path}")
        return path

    def list_profiles(self) -> list[str]:
        if not self.profiles_dir.exists():
            return []
        return sorted((p.name for p in self.profiles_dir.iterdir() if p.is_dir()), key=str.casefold)

    def profile_files(self, requested: str | None = None) -> tuple[Path, ModList, PluginList]:
        path = self.profile_path(requested)
        return path, ModList.read(path / "modlist.txt"), PluginList.read(path / "plugins.txt")

    def mod_path(self, name: str) -> Path:
        _safe_name(name)
        path = next((item for item in self.mods_dir.iterdir() if item.name.casefold() == name.casefold()), None) if self.mods_dir.exists() else None
        if path is None or not path.is_dir():
            raise Mo2Error(f"Mod not found: {name}")
        return path

    def _listed_mod_dir(self, name: str) -> Path | None:
        try:
            _safe_name(name)
        except Mo2Error:
            return None
        candidate = self.mods_dir / name
        if not candidate.resolve().is_relative_to(self.mods_dir.resolve()):
            return None
        return candidate

    def mod_metadata(self, name: str) -> ModMetadata:
        return ModMetadata.read(self.mod_path(name))

    def profile_settings(self, requested: str | None = None) -> dict[str, object]:
        path = self.profile_path(requested)
        document = IniDocument.read(path / "settings.ini")
        return {"LocalSettings": bool(document.get("LocalSettings", False)), "LocalSaves": bool(document.get("LocalSaves", False))}

    def set_profile_settings(self, requested: str | None = None, local_inis: bool | None = None, local_saves: bool | None = None) -> dict[str, object]:
        path = self.profile_path(requested)
        document = IniDocument.read(path / "settings.ini")
        current = self.profile_settings(requested)
        if local_inis is not None:
            document.set("LocalSettings", local_inis)
            current["LocalSettings"] = local_inis
        if local_saves is not None:
            saves = path / "saves"
            hidden = path / "_saves"
            if local_saves and hidden.exists() and not saves.exists():
                hidden.rename(saves)
            elif not local_saves and saves.exists() and not hidden.exists():
                saves.rename(hidden)
            document.set("LocalSaves", local_saves)
            current["LocalSaves"] = local_saves
        document.write(path / "settings.ini")
        return current

    def executables(self) -> list[dict[str, object]]:
        document = IniDocument.read(self.ini)
        values = document.as_dict("customExecutables")
        grouped: dict[str, dict[str, object]] = {}
        for key, value in values.items():
            if "\\" not in key:
                continue
            index, field = key.split("\\", 1)
            if index.isdigit():
                grouped.setdefault(index, {})[field] = value
        return [grouped[index] | {"index": int(index)} for index in sorted(grouped, key=lambda item: int(item))]

    def write_selected_profile(self, name: str) -> None:
        _safe_name(name)
        if not self.ini.exists():
            raise Mo2Error("ModOrganizer.ini is required to write selected profile.")
        content = self.ini.read_text(encoding="utf-8-sig")
        pattern = re.compile(r"(?im)^(\s*selected_profile\s*=\s*).*$")
        if pattern.search(content):
            content = pattern.sub(rf"\g<1>{name}", content, count=1)
        else:
            section = re.search(r"(?im)^\[General\]\s*$", content)
            if not section:
                content = content.rstrip("\r\n") + "\r\n\r\n[General]\r\n"
            else:
                start = section.end()
                next_section = re.search(r"(?im)^\[.+?\]\s*$", content[start:])
                insert_at = start + (next_section.start() if next_section else len(content[start:]))
                content = content[:insert_at].rstrip("\r\n") + f"\r\nselected_profile={name}\r\n" + content[insert_at:]
        self.ini.write_text(content, encoding="utf-8", newline="")
        self.selected_profile = name

    def create_profile(self, name: str, source: str | None = None) -> Path:
        _safe_name(name)
        destination = self.profiles_dir / name
        if destination.exists():
            raise Mo2Error(f"Profile already exists: {name}")
        destination.mkdir(parents=True, exist_ok=False)
        if source:
            source_path = self.profile_path(source)
            for item in source_path.iterdir():
                if item.is_file():
                    shutil.copy2(item, destination / item.name)
                elif item.name == ".mo2cli" and item.is_dir():
                    shutil.copytree(item, destination / item.name)
        if not (destination / "modlist.txt").exists():
            write_text(destination / "modlist.txt", "# This file was automatically generated by Mod Organizer.\r\n")
        if not (destination / "archives.txt").exists():
            write_text(destination / "archives.txt", "")
        return destination

    def delete_profile(self, name: str) -> None:
        path = self.profile_path(name)
        if len(self.list_profiles()) <= 1:
            raise Mo2Error("Cannot delete the last remaining profile.")
        shutil.rmtree(path)

    def snapshot(self, requested: str | None = None) -> dict:
        path, mods, plugins = self.profile_files(requested)
        loadorder = PluginList.read(path / "loadorder.txt")
        return {
            "instance": str(self.root),
            "game": self.game_name,
            "game_path": str(self.game_path) if self.game_path else None,
            "profile": path.name,
            "paths": {"base": str(self.base), "mods": str(self.mods_dir), "profiles": str(self.profiles_dir), "downloads": str(self.downloads_dir)},
            "mods": [{"name": e.name, "enabled": e.enabled, "foreign": e.foreign} for e in mods.entries],
            "plugins": [{"name": e.name, "enabled": e.enabled} for e in plugins.entries],
            "loadorder": [e.name for e in loadorder.entries],
        }

    def file_owners(self, requested: str | None, relative: str) -> list[dict]:
        _, modlist, _ = self.profile_files(requested)
        parts = _safe_relative_path(relative)
        wanted = "/".join(parts).casefold()
        owners: list[dict] = []
        overwrite_file = self.overwrite_dir.joinpath(*parts)
        if overwrite_file.is_file():
            owners.append({"mod": "overwrite", "path": str(overwrite_file), "priority": -1, "ui_index": -1, "file_index": -1})
        count = len(modlist.entries)
        for file_index, entry in enumerate(modlist.entries):
            if not entry.enabled or entry.foreign:
                continue
            root = self._listed_mod_dir(entry.name)
            if root is None:
                continue
            if not root.is_dir():
                continue
            for file in root.rglob("*"):
                if file.is_file() and "/".join(file.relative_to(root).parts).casefold() == wanted:
                    ui_index = count - 1 - file_index
                    owners.append({"mod": entry.name, "path": str(file), "priority": file_index, "ui_index": ui_index, "file_index": file_index})
        return owners

    def conflicts(self, requested: str | None = None) -> list[dict]:
        _, modlist, _ = self.profile_files(requested)
        providers: dict[str, list[str]] = {}

        def is_virtual_file(file: Path, root: Path) -> bool:
            relative_parts = file.relative_to(root).parts
            if len(relative_parts) == 1 and relative_parts[0].casefold() in {"meta.ini", "readme.txt"}:
                return False
            return not (relative_parts and relative_parts[0].casefold() == "fomod")

        if self.overwrite_dir.is_dir():
            for file in self.overwrite_dir.rglob("*"):
                if file.is_file() and is_virtual_file(file, self.overwrite_dir):
                    key = "/".join(file.relative_to(self.overwrite_dir).parts).casefold()
                    providers.setdefault(key, []).insert(0, "overwrite")
        for priority, entry in enumerate(modlist.entries):
            if not entry.enabled or entry.foreign:
                continue
            root = self._listed_mod_dir(entry.name)
            if root is None:
                continue
            if not root.is_dir():
                continue
            for file in root.rglob("*"):
                if file.is_file() and is_virtual_file(file, root):
                    key = "/".join(file.relative_to(root).parts).casefold()
                    providers.setdefault(key, []).append(entry.name)
        return [{"path": path, "providers": names, "winner": names[0]} for path, names in sorted(providers.items()) if len(names) > 1]

    def virtual_files(self, requested: str | None = None) -> list[dict[str, object]]:
        _, modlist, _ = self.profile_files(requested)
        providers: dict[str, list[dict[str, object]]] = {}

        def add_root(root: Path, name: str, file_index: int) -> None:
            if not root.is_dir():
                return
            ui_index = len(modlist.entries) - 1 - file_index
            for file in root.rglob("*"):
                if file.is_file():
                    relative_parts = file.relative_to(root).parts
                    if len(relative_parts) == 1 and relative_parts[0].casefold() in {"meta.ini", "readme.txt"}:
                        continue
                    if relative_parts and relative_parts[0].casefold() == "fomod":
                        continue
                    relative = "/".join(file.relative_to(root).parts).casefold()
                    providers.setdefault(relative, []).append({"mod": name, "path": str(file), "relative": "/".join(file.relative_to(root).parts), "priority": file_index, "ui_index": ui_index, "file_index": file_index, "size": file.stat().st_size})

        add_root(self.overwrite_dir, "overwrite", -1)
        for file_index, entry in enumerate(modlist.entries):
            if entry.enabled and not entry.foreign:
                root = self._listed_mod_dir(entry.name)
                if root is not None:
                    add_root(root, entry.name, file_index)
        return [{"path": path, "winner": sources[0], "providers": sources} for path, sources in sorted(providers.items())]

    def active_mod_roots(self, requested: str | None = None) -> list[Path]:
        """Return active mod roots in overlay order, from lowest to highest priority."""
        _, modlist, _ = self.profile_files(requested)
        roots: list[Path] = []
        for entry in reversed(modlist.entries):
            if not entry.enabled or entry.foreign:
                continue
            root = self._listed_mod_dir(entry.name)
            if root is not None and root.is_dir():
                roots.append(root)
        if self.overwrite_dir.is_dir():
            roots.append(self.overwrite_dir)
        return roots

    def materialize(
        self,
        requested: str | None,
        destination: str | Path,
        replace: bool = False,
        include_prefixes: tuple[str, ...] | None = None,
    ) -> dict[str, object]:
        target_root = Path(destination).expanduser().resolve()
        if target_root.exists() and any(target_root.iterdir()):
            if not replace:
                raise Mo2Error(f"Destination directory is not empty; use --replace to overwrite: {target_root}")
            shutil.rmtree(target_root)
        target_root.mkdir(parents=True, exist_ok=True)
        count = 0
        for item in self.virtual_files(requested):
            if include_prefixes:
                normalized = str(item["path"]).replace("\\", "/").casefold().lstrip("/")
                prefixes = tuple(prefix.replace("\\", "/").casefold().lstrip("/").rstrip("/") + "/" for prefix in include_prefixes)
                if not any(normalized.startswith(prefix) for prefix in prefixes):
                    continue
            winner = item["winner"]
            relative = str(winner.get("relative") or item["path"])
            target = target_root.joinpath(*Path(relative).parts)
            if not target.resolve().is_relative_to(target_root):
                raise Mo2Error(f"Virtual file path escapes destination directory: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(winner["path"], target)
            count += 1
        return {"destination": str(target_root), "files": count}

    def doctor(self, requested: str | None = None) -> list[dict]:
        profile, mods, plugins = self.profile_files(requested)
        issues: list[dict] = []
        seen: set[str] = set()
        for entry in mods.entries:
            key = entry.name.casefold()
            if key in seen:
                issues.append({"level": "error", "code": "duplicate-mod", "message": entry.name})
            seen.add(key)
            if entry.foreign:
                continue
            if self._listed_mod_dir(entry.name) is None:
                issues.append({"level": "error", "code": "unsafe-mod-name", "message": entry.name})
            elif not self._listed_mod_dir(entry.name).is_dir():
                issues.append({"level": "warning", "code": "missing-mod", "message": entry.name})
        seen.clear()
        for entry in plugins.entries:
            key = entry.name.casefold()
            if key in seen:
                issues.append({"level": "error", "code": "duplicate-plugin", "message": entry.name})
            seen.add(key)
        installed = {path.name.casefold() for path in self.mods_dir.iterdir() if path.is_dir()} if self.mods_dir.exists() else set()
        listed = {entry.name.casefold() for entry in mods.entries}
        for name in sorted(installed - listed):
            issues.append({"level": "warning", "code": "unlisted-mod", "message": name})
        for required in ("modlist.txt",):
            if not (profile / required).exists():
                issues.append({"level": "error", "code": "missing-file", "message": required})
        if self.downloads_dir.is_dir():
            mod_ids: dict[str, str] = {}
            for entry in mods.entries:
                if entry.foreign or not entry.enabled:
                    continue
                mdir = self._listed_mod_dir(entry.name)
                if mdir and mdir.is_dir():
                    m_meta = mdir / "meta.ini"
                    if m_meta.is_file():
                        try:
                            mdoc = IniDocument.read(m_meta)
                            mid = mdoc.get("modID", section="General") or mdoc.get("modid", section="General") or mdoc.get("modid")
                            if mid:
                                mod_ids[str(mid)] = entry.name
                        except Exception:
                            pass
            if self.mods_dir.is_dir():
                for mdir in self.mods_dir.iterdir():
                    if mdir.is_dir() and not mdir.name.casefold().endswith("_separator"):
                        m_meta = mdir / "meta.ini"
                        if m_meta.is_file():
                            try:
                                mdoc = IniDocument.read(m_meta)
                                mid = mdoc.get("modID", section="General") or mdoc.get("modid", section="General") or mdoc.get("modid")
                                if not mid and self.downloads_dir.is_dir():
                                    inst_file = mdoc.get("installationFile", section="General") or mdoc.get("installationFile")
                                    if inst_file:
                                        inst_meta = self.downloads_dir / f"{inst_file}.meta"
                                        if inst_meta.is_file():
                                            try:
                                                inst_sdoc = IniDocument.read(inst_meta)
                                                mid = inst_sdoc.get("modID", section="General") or inst_sdoc.get("modid", section="General")
                                            except Exception:
                                                pass
                                if mid and str(mid) not in mod_ids:
                                    mod_ids[str(mid)] = mdir.name
                            except Exception:
                                pass
            for sidecar in self.downloads_dir.glob("*.meta"):
                try:
                    sdoc = IniDocument.read(sidecar)
                    inst = sdoc.get("installed", section="General")
                    if inst is False or str(inst).strip().lower() == "false":
                        smid = sdoc.get("modID", section="General") or sdoc.get("modid", section="General")
                        if smid and str(smid) in mod_ids:
                            issues.append({
                                "level": "warning",
                                "code": "uninstalled-download",
                                "message": f"{sidecar.name[:-5]} (modID {smid}) belongs to installed mod '{mod_ids[str(smid)]}' but is marked uninstalled",
                            })
                except Exception:
                    pass
        return issues

    def json_snapshot(self, requested: str | None = None) -> str:
        return json.dumps(self.snapshot(requested), ensure_ascii=False, indent=2)
