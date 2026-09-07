from __future__ import annotations

import struct
import datetime as dt
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .formats import PluginEntry, PluginList, write_text
from .workspace import Instance, Mo2Error


@dataclass
class PluginHeader:
    path: Path
    name: str
    masters: list[str] = field(default_factory=list)
    flags: int = 0
    form_version: int | None = None
    parse_error: str | None = None

    @property
    def is_master(self) -> bool:
        return bool(self.flags & 0x1) or self.name.casefold().endswith(".esm")

    @property
    def is_light(self) -> bool:
        return bool(self.flags & 0x200) or self.name.casefold().endswith(".esl")


def parse_header(path: Path) -> PluginHeader:
    result = PluginHeader(path, path.name)
    try:
        data = path.read_bytes()
        if len(data) < 24 or data[:4] != b"TES4":
            result.parse_error = "TES4 header not found"
            return result
        size = struct.unpack_from("<I", data, 4)[0]
        result.flags = struct.unpack_from("<I", data, 8)[0]
        result.form_version = struct.unpack_from("<H", data, 20)[0]
        payload = data[24 : 24 + size]
        offset = 0
        while offset + 6 <= len(payload):
            signature = payload[offset : offset + 4]
            sub_size = struct.unpack_from("<H", payload, offset + 4)[0]
            start = offset + 6
            end = start + sub_size
            if end > len(payload):
                break
            value = payload[start:end]
            if signature == b"MAST":
                result.masters.append(value.rstrip(b"\0").decode("utf-8", errors="replace"))
            offset = end
    except OSError as error:
        result.parse_error = str(error)
    return result


def _active_plugin_files(instance: Instance, profile: str | None) -> dict[str, Path]:
    _, modlist, _ = instance.profile_files(profile)
    result: dict[str, Path] = {}
    roots: list[Path] = []
    if instance.overwrite_dir.is_dir():
        roots.append(instance.overwrite_dir)
    for entry in modlist.entries:
        if entry.enabled and not entry.foreign:
            root = instance._listed_mod_dir(entry.name)
            if root is None:
                continue
            if root.is_dir():
                roots.append(root)
    if instance.game_path and instance.game_path.is_dir():
        game_data = instance.game_path / "Data"
        roots.append(game_data if game_data.is_dir() else instance.game_path)
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.casefold() in {".esm", ".esp", ".esl"}:
                result.setdefault(path.name.casefold(), path)
    return result


def sync_plugin_lists(instance: Instance, profile: str | None = None) -> dict[str, object]:
    profile_path = instance.profile_path(profile)
    plugins_path = profile_path / "plugins.txt"
    loadorder_path = profile_path / "loadorder.txt"
    plugins = PluginList.read(plugins_path)
    loadorder = PluginList.read(loadorder_path)
    active_files = _active_plugin_files(instance, profile)
    known_plugins = {entry.name.casefold() for entry in plugins.entries}
    known_loadorder = {entry.name.casefold() for entry in loadorder.entries}
    missing = [path.name for key, path in active_files.items() if key not in known_plugins or key not in known_loadorder]
    missing = list(dict.fromkeys(missing))
    enable_existing = [entry for entry in plugins.entries if entry.name.casefold() in active_files and not entry.enabled]
    if not missing and not enable_existing:
        return {"profile": profile_path.name, "added": [], "changed": False}
    backup_dir = profile_path / ".mo2cli-backups" / f"plugins-sync-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for path in (plugins_path, loadorder_path):
        if path.exists():
            shutil.copy2(path, backup_dir / path.name)
    for name in missing:
        if plugins.find(name) is None:
            plugins.lines.append(PluginEntry(name, True))
        if loadorder.find(name) is None:
            loadorder.lines.append(PluginEntry(name, False))
    for entry in enable_existing:
        entry.enabled = True
    headers = {key: parse_header(path) for key, path in active_files.items()}
    for _ in range(len(loadorder.entries) + 1):
        changed = False
        order = [entry.name for entry in loadorder.entries]
        positions = {name.casefold(): index for index, name in enumerate(order)}
        for child in order:
            header = headers.get(child.casefold())
            if header is None:
                continue
            child_index = positions.get(child.casefold())
            if child_index is None:
                continue
            for master in header.masters:
                master_index = positions.get(master.casefold())
                if master_index is not None and master_index > child_index:
                    loadorder.move(master, child_index)
                    changed = True
                    break
            if changed:
                break
        if not changed:
            break
    write_text(plugins_path, plugins.render())
    write_text(loadorder_path, loadorder.render())
    return {"profile": profile_path.name, "added": missing, "enabled": [entry.name for entry in enable_existing], "changed": True, "backup": str(backup_dir)}


def remove_plugin_entries(instance: Instance, profile: str | None, names: list[str]) -> dict[str, object]:
    """Remove stale plugin names from both profile plugin lists."""
    profile_path = instance.profile_path(profile)
    wanted = {name.casefold() for name in names}
    if not wanted:
        raise Mo2Error("At least one plugin name is required.")
    backup_dir = profile_path / ".mo2cli-backups" / f"plugins-remove-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    changed = False
    removed: list[str] = []
    for filename in ("plugins.txt", "loadorder.txt"):
        path = profile_path / filename
        model = PluginList.read(path)
        if path.exists():
            shutil.copy2(path, backup_dir / filename)
        for entry in list(model.entries):
            if entry.name.casefold() in wanted:
                if entry.name not in removed:
                    removed.append(entry.name)
                model.remove(entry.name)
                changed = True
        write_text(path, model.render())
    return {"profile": profile_path.name, "removed": removed, "changed": changed, "backup": str(backup_dir)}


def analyze(instance: Instance, profile: str | None = None) -> list[dict[str, object]]:
    profile_path, _, enabled_list = instance.profile_files(profile)
    loadorder = PluginList.read(profile_path / "loadorder.txt")
    order = [entry.name for entry in loadorder.entries]
    if not order:
        order = [entry.name for entry in enabled_list.entries]
    enabled = {entry.name.casefold(): entry.enabled for entry in enabled_list.entries}
    files = _active_plugin_files(instance, profile)
    parsed = {name: parse_header(path) for name, path in files.items()}
    position = {name.casefold(): index for index, name in enumerate(order)}
    issues: list[dict[str, object]] = []
    for index, name in enumerate(order):
        key = name.casefold()
        if key not in files:
            issues.append({"level": "error", "code": "missing-plugin-file", "plugin": name})
            continue
        header = parsed[key]
        if header.parse_error:
            issues.append({"level": "warning", "code": "unparsed-plugin", "plugin": name, "message": header.parse_error})
        for master in header.masters:
            master_key = master.casefold()
            if master_key not in files:
                issues.append({"level": "error", "code": "missing-master", "plugin": name, "master": master})
            elif master_key not in position:
                issues.append({"level": "error", "code": "master-not-in-loadorder", "plugin": name, "master": master})
            elif position[master_key] > index:
                issues.append({"level": "error", "code": "master-after-plugin", "plugin": name, "master": master})
            elif enabled.get(master_key, True) is False and enabled.get(key, False):
                issues.append({"level": "error", "code": "disabled-master", "plugin": name, "master": master})
    graph = {name.casefold(): [master.casefold() for master in parsed[name.casefold()].masters] for name in order if name.casefold() in parsed}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, trail: list[str]) -> None:
        if node in visiting:
            cycle = trail[trail.index(node):] + [node] if node in trail else trail + [node]
            issues.append({"level": "error", "code": "master-cycle", "plugins": cycle})
            return
        if node in visited:
            return
        visiting.add(node)
        for parent in graph.get(node, []):
            visit(parent, trail + [node])
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node, [])
    for name in enabled:
        if enabled[name] and name not in position:
            issues.append({"level": "warning", "code": "enabled-not-in-loadorder", "plugin": name})
    return issues


def catalog(instance: Instance, profile: str | None = None) -> list[dict[str, object]]:
    profile_path, _, enabled_list = instance.profile_files(profile)
    loadorder = PluginList.read(profile_path / "loadorder.txt")
    enabled = {entry.name.casefold(): entry.enabled for entry in enabled_list.entries}
    order = [entry.name for entry in loadorder.entries]
    order += [entry.name for entry in enabled_list.entries if entry.name.casefold() not in {name.casefold() for name in order}]
    files = _active_plugin_files(instance, profile)
    result = []
    for index, name in enumerate(order):
        header = parse_header(files[name.casefold()]) if name.casefold() in files else None
        result.append({
            "index": index,
            "name": name,
            "enabled": enabled.get(name.casefold(), False),
            "path": str(header.path) if header else None,
            "masters": header.masters if header else [],
            "is_master": header.is_master if header else None,
            "is_light": header.is_light if header else None,
            "form_version": header.form_version if header else None,
            "parse_error": header.parse_error if header else "file missing",
        })
    return result


def sort_with_loot(instance: Instance, profile: str | None = None, loot_exe: str | None = None, dry_run: bool = False) -> dict[str, object]:
    if instance.game_path is None:
        raise Mo2Error("gamePath not found in instance.")
    profile_path, _, _ = instance.profile_files(profile)
    executable = Path(loot_exe).expanduser() if loot_exe else instance.root / "loot" / "lootcli.exe"
    if not executable.is_file():
        raise Mo2Error(f"lootcli.exe not found: {executable}")
    loadorder = profile_path / "loadorder.txt"
    if not loadorder.exists():
        raise Mo2Error(f"loadorder.txt not found: {loadorder}")
    loot_game_ids = {
        "skyrim special edition": "skyrimse",
        "skyrim special edition (steam)": "skyrimse",
    }
    loot_game = loot_game_ids.get(instance.game_name.casefold().strip(), instance.game_name)
    with tempfile.TemporaryDirectory(prefix="mo2cli-loot-") as temporary:
        game_root = Path(temporary) / "game"
        data_root = game_root / "Data"
        data_root.mkdir(parents=True, exist_ok=True)
        active_files = _active_plugin_files(instance, profile)
        if not active_files:
            raise Mo2Error("No active plugin files found; LOOT was not run.")
        for plugin_path in active_files.values():
            shutil.copy2(plugin_path, data_root / plugin_path.name)
        staged_list = Path(temporary) / "loadorder.txt"
        shutil.copy2(loadorder, staged_list)
        report = Path(temporary) / "report.json"
        command = [str(executable), "--game", loot_game, "--gamePath", str(game_root), "--pluginListPath", str(staged_list), "--out", str(report), "--skipUpdateMasterlist"]
        child_env = os.environ.copy()
        bundled_dlls = instance.root / "dlls"
        if bundled_dlls.is_dir():
            child_env["PATH"] = str(bundled_dlls) + os.pathsep + child_env.get("PATH", "")
        result = subprocess.run(command, cwd=str(executable.parent), env=child_env, capture_output=True, text=True)
        if result.returncode:
            raise Mo2Error(f"LOOT failed: {result.stderr.strip() or result.stdout.strip()}")
        if not staged_list.exists():
            raise Mo2Error("LOOT did not produce a sorted loadorder output.")
        original_model = PluginList.read(loadorder)
        sorted_model = PluginList.read(staged_list)
        original_names = {entry.name.casefold() for entry in original_model.entries}
        sorted_names = {entry.name.casefold() for entry in sorted_model.entries}
        if original_names != sorted_names:
            missing = sorted(original_names - sorted_names)
            added = sorted(sorted_names - original_names)
            raise Mo2Error(
                "LOOT modified the set of plugins; loadorder was not written. "
                f"Missing: {missing[:10]} Added: {added[:10]}"
            )
        backup = profile_path / ".mo2cli-backups" / f"loadorder-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
        if not dry_run:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(loadorder, backup)
            write_text(loadorder, sorted_model.render())
        report_data = None
        if report.exists():
            try:
                report_data = json.loads(report.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                report_data = {"raw": report.read_text(encoding="utf-8", errors="replace")}
        return {"loot": str(executable), "profile": profile_path.name, "game": loot_game, "loadorder": str(loadorder), "plugins": len(sorted_model.entries), "backup": str(backup) if not dry_run else None, "dry_run": dry_run, "report": report_data}
