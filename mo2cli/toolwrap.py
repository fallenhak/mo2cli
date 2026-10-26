"""Headless wrappers for xEdit (quick auto clean), lootcli and BSArch.

Flags come from the verified notes in ``Araclar-otomasyon.md``. xEdit runs
inside USVFS through :func:`usvfs.run` (the same mechanism as ``tools
automate``); lootcli and BSArch work on real paths and run as plain
subprocesses. Output that tools drop into ``overwrite`` can be moved into a
named mod with ``capture_new_files`` (``--capture-to``).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .formats import PluginList, read_text_exact, write_text
from .journal import record
from .plugins import _active_plugin_files
from .tool_workflows import ensure_output_mod
from .usvfs import run as run_vfs
from .workspace import Instance, Mo2Error

XEDIT_NAMES = ("sseedit", "sseedit64", "xedit", "xedit64", "tes5edit", "tes5edit64")
LOOT_NAMES = ("lootcli", "loot")
BSARCH_NAMES = ("bsarch", "bsarch64")
VANILLA_PLUGINS = {"skyrim.esm", "update.esm", "dawnguard.esm", "hearthfires.esm", "dragonborn.esm"}


def find_tool(instance: Instance, explicit: str | None, names: tuple[str, ...], default_paths: list[Path] | None = None) -> Path:
    """Explicit ``--exe`` first, then a registered MO2 executable by title or file name, then default paths."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise Mo2Error(f"Executable not found: {path}")
        return path.resolve()
    for item in instance.executables():
        binary = Path(str(item.get("binary", "")))
        labels = {str(item.get("title", "")).casefold(), binary.stem.casefold()}
        if labels & set(names) and binary.is_file():
            return binary.resolve()
    for path in default_paths or []:
        if path.is_file():
            return path.resolve()
    raise Mo2Error(f"Tool not found ({'/'.join(names)}). Register it with 'tools register' or pass --exe.")


# --- overwrite capture ------------------------------------------------------------------


def snapshot_overwrite(instance: Instance) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    if instance.overwrite_dir.is_dir():
        for path in instance.overwrite_dir.rglob("*"):
            if path.is_file():
                stat = path.stat()
                result["/".join(path.relative_to(instance.overwrite_dir).parts)] = (stat.st_size, stat.st_mtime_ns)
    return result


def capture_new_files(instance: Instance, profile: str | None, mod: str, before: dict[str, tuple[int, int]]) -> dict[str, object]:
    """Move files that are new or changed in ``overwrite`` since ``before`` into mod ``mod`` (journaled)."""
    after = snapshot_overwrite(instance)
    changed = sorted(name for name, state in after.items() if before.get(name) != state)
    result: dict[str, object] = {"mod": mod, "moved": [], "count": 0}
    if not changed:
        return result
    existed = (instance.mods_dir / mod).is_dir()
    target = ensure_output_mod(instance, profile, mod)
    trash = instance.base / ".mo2cli-trash" / f"capture-{dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
    moved = []
    for name in changed:
        source = instance.overwrite_dir / name
        destination = target / name
        replaced = None
        if destination.exists():
            replaced = trash / name
            replaced.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(destination), str(replaced))
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        moved.append({"relative": name, "from": str(source), "to": str(destination), "replaced": str(replaced) if replaced else None})
    entry = record(instance, "overwrite_capture", mod=mod, mod_created=not existed, moved=moved)
    result.update({"moved": [item["relative"] for item in moved], "count": len(moved), "journal_id": entry["id"]})
    return result


# --- xEdit ---------------------------------------------------------------------------------

_LOG_PREFIX = r"^\s*(?:\[[\d:.]+\]\s*)?"


def parse_xedit_log(text: str) -> dict[str, object]:
    """Count cleaning actions in an xEdit quick-clean log.

    ``Removing:`` lines are identical-to-master (ITM) records, ``Undeleting:``
    lines are deleted references (UDR) that were undeleted and disabled,
    ``Skipping:`` lines were left alone. Summary lines are used when the log
    carries them. The exact wording has not been verified against a real run.
    """
    counts = {"itm": 0, "udr": 0, "skipped": 0}
    for line in text.splitlines():
        if re.match(_LOG_PREFIX + r"Removing:", line, re.IGNORECASE):
            counts["itm"] += 1
        elif re.match(_LOG_PREFIX + r"Undeleting:", line, re.IGNORECASE):
            counts["udr"] += 1
        elif re.match(_LOG_PREFIX + r"Skipping:", line, re.IGNORECASE):
            counts["skipped"] += 1
    summary: dict[str, int] = {}
    for key, pattern in (
        ("itm", r"(?:removed|identical to master)[^\n\d]{0,40}?(?:records?)?\s*[:=]\s*(\d+)"),
        ("udr", r"undeleted[^\n\d]{0,40}?(?:records?|references?)?\s*[:=]\s*(\d+)"),
        ("skipped", r"skipped[^\n\d]{0,40}?(?:records?)?\s*[:=]\s*(\d+)"),
    ):
        found = re.findall(pattern, text, re.IGNORECASE)
        if found:
            summary[key] = max(int(item) for item in found)
    return {
        "itm": summary.get("itm", counts["itm"]),
        "udr": summary.get("udr", counts["udr"]),
        "skipped": summary.get("skipped", counts["skipped"]),
        "source": "summary" if summary else "lines",
        "finished": bool(re.search(r"(cleaning|quick auto clean)[^\n]*(done|complete|finished)|\bdone\b", text, re.IGNORECASE)),
    }


def _newest_log(directory: Path, since: float) -> Path | None:
    if not directory.is_dir():
        return None
    logs = [path for path in directory.iterdir() if path.is_file() and path.suffix.casefold() == ".txt" and path.stat().st_mtime >= since - 2]
    return max(logs, key=lambda path: path.stat().st_mtime, default=None)


def xedit_clean(
    instance: Instance,
    profile: str | None,
    plugin: str,
    exe: str | None = None,
    log_dir: str | None = None,
    capture_to: str | None = None,
    usvfs_dir: str | None = None,
    moprofile: bool = False,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, object]:
    active = _active_plugin_files(instance, profile)
    source = active.get(plugin.casefold())
    if source is None:
        raise Mo2Error(f"Plugin not found among active mods or game Data: {plugin}")
    folded = plugin.casefold()
    if (folded in VANILLA_PLUGINS or re.match(r"cc.*\.(esm|esl|esp)$", folded)) and not force:
        raise Mo2Error(f"{plugin} is a vanilla or Creation Club plugin; cleaning it is normally wrong. Use --force to override.")
    binary = find_tool(instance, exe, XEDIT_NAMES)
    profile_path = instance.profile_path(profile)
    logs = Path(log_dir).expanduser() if log_dir else instance.base / "Tools" / "xedit-logs"
    data = instance.game_path / "Data" if instance.game_path and (instance.game_path / "Data").is_dir() else instance.game_path
    if data is None:
        raise Mo2Error("gamePath not found in instance.")
    arguments = ["-SSE", "-qac", "-autoexit", "-autoload", "-IKnowWhatImDoing", f"-D:{data}", f"-P:{profile_path / 'plugins.txt'}"]
    if (profile_path / "Skyrim.ini").is_file():
        arguments.append(f"-I:{profile_path / 'Skyrim.ini'}")
    if moprofile:
        arguments.append(f"-moprofile:{profile_path.name}")
    arguments += [f"-R:{logs}{os.sep}", plugin]
    result: dict[str, object] = {"plugin": plugin, "source_file": str(source), "binary": str(binary), "arguments": arguments, "log_dir": str(logs), "dry_run": dry_run, "virtualization": "usvfs"}
    if dry_run:
        return result
    logs.mkdir(parents=True, exist_ok=True)
    backup = instance.base / ".mo2cli-trash" / f"xedit-clean-{dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')}" / source.name
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, backup)
    source_state = (source.stat().st_size, source.stat().st_mtime_ns)
    before = snapshot_overwrite(instance)
    started = time.time()
    run = run_vfs(instance, profile, str(binary), arguments, usvfs_dir, str(data), direct_mods=True)
    result["returncode"] = run.get("returncode")
    result["backup"] = str(backup)
    result["modified_in_place"] = (source.stat().st_size, source.stat().st_mtime_ns) != source_state
    log = _newest_log(logs, started)
    if log is not None:
        result["log"] = str(log)
        result.update(parse_xedit_log(log.read_text(encoding="utf-8", errors="replace")))
    else:
        result["log"] = None
    result["ok"] = run.get("returncode") in (0, None) and log is not None
    if capture_to:
        result["capture"] = capture_new_files(instance, profile, capture_to, before)
    return result


# --- LOOT ----------------------------------------------------------------------------------


def _stage_plugin(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def apply_load_order(instance: Instance, profile: str | None, names: list[str]) -> dict[str, object]:
    """Write ``names`` as loadorder.txt and reorder plugins.txt to match (journaled, undoable)."""
    profile_path = instance.profile_path(profile)
    loadorder_path, plugins_path = profile_path / "loadorder.txt", profile_path / "plugins.txt"
    backups = [{"path": str(path), "content": read_text_exact(path) if path.exists() else ""} for path in (loadorder_path, plugins_path)]
    rank = {name.casefold(): index for index, name in enumerate(names)}

    def reorder(model: PluginList) -> None:
        queue = iter(sorted((entry for entry in model.entries if entry.name.casefold() in rank), key=lambda entry: rank[entry.name.casefold()]))
        model.lines = [next(queue) if not isinstance(line, str) and line.name.casefold() in rank else line for line in model.lines]

    model = PluginList.read(loadorder_path)
    reorder(model)
    write_text(loadorder_path, model.render())
    plugins = PluginList.read(plugins_path)
    reorder(plugins)
    write_text(plugins_path, plugins.render())
    entry = record(instance, "loot_apply", profiles=backups)
    return {"journal_id": entry["id"], "loadorder": str(loadorder_path), "plugins": str(plugins_path)}


def summarize_loot_report(report: dict[str, object]) -> dict[str, object]:
    messages = [item for item in report.get("messages", []) if isinstance(item, dict)] if isinstance(report.get("messages"), list) else []
    dirty, missing, plugin_messages = [], [], []
    for plugin in report.get("plugins", []) if isinstance(report.get("plugins"), list) else []:
        if not isinstance(plugin, dict):
            continue
        name = plugin.get("name")
        for item in plugin.get("dirty") or []:
            if isinstance(item, dict):
                dirty.append({"plugin": name, "utility": item.get("cleaningUtility"), "itm": item.get("itm"), "udr": item.get("deletedReferences"), "nav": item.get("deletedNavmesh")})
        for master in plugin.get("missingMasters") or []:
            missing.append({"plugin": name, "master": master})
        for item in plugin.get("messages") or []:
            if isinstance(item, dict):
                plugin_messages.append({"plugin": name, "type": item.get("type"), "text": item.get("text")})
    return {"messages": messages, "plugin_messages": plugin_messages, "dirty": dirty, "missing_masters": missing}


def loot_sort(
    instance: Instance,
    profile: str | None,
    exe: str | None = None,
    game: str | None = None,
    apply: bool = False,
    update_masterlist: bool = False,
    dry_run: bool = False,
) -> dict[str, object]:
    """Sort with lootcli into a temporary output file; loadorder.txt is only touched by ``apply``."""
    profile_path = instance.profile_path(profile)
    loadorder = profile_path / "loadorder.txt"
    if not loadorder.is_file():
        raise Mo2Error(f"loadorder.txt not found: {loadorder}")
    binary = find_tool(instance, exe, LOOT_NAMES, [instance.root / "loot" / "lootcli.exe", instance.base / "loot" / "lootcli.exe"])
    loot_game = game or ("SkyrimSE" if instance.game_name.casefold().startswith("skyrim special edition") else instance.game_name)
    files = _active_plugin_files(instance, profile)
    if not files:
        raise Mo2Error("No active plugin files found; LOOT was not run.")
    result: dict[str, object] = {"loot": str(binary), "game": loot_game, "profile": profile_path.name, "plugins": len(files), "dry_run": dry_run, "applied": False}
    if dry_run:
        result["note"] = "Dry run: lootcli was not started."
        return result
    with tempfile.TemporaryDirectory(prefix="mo2cli-loot-") as temporary:
        root = Path(temporary)
        data = root / "game" / "Data"
        data.mkdir(parents=True)
        for path in files.values():
            _stage_plugin(path, data / path.name)
        staged_in, staged_out, report_path = root / "loadorder.txt", root / "sorted-loadorder.txt", root / "report.json"
        shutil.copy2(loadorder, staged_in)
        command = [str(binary), "--game", loot_game, "--gamePath", str(root / "game"), "--pluginListPath", str(staged_in), "--out", str(report_path), "--pluginListOutputPath", str(staged_out), "--language", "en"]
        if not update_masterlist:
            command.append("--skipUpdateMasterlist")
        environment = os.environ.copy()
        dlls = instance.root / "dlls"
        if dlls.is_dir():
            environment["PATH"] = str(dlls) + os.pathsep + environment.get("PATH", "")
        completed = subprocess.run(command, cwd=str(binary.parent), env=environment, capture_output=True, text=True)
        result["command"] = command
        result["returncode"] = completed.returncode
        if completed.returncode:
            raise Mo2Error(f"LOOT failed: {(completed.stderr or completed.stdout).strip()[:500]}")
        if not staged_out.is_file():
            raise Mo2Error("LOOT did not write --pluginListOutputPath; load order untouched.")
        sorted_names = [entry.name for entry in PluginList.read(staged_out).entries]
        current = [entry.name for entry in PluginList.read(loadorder).entries]
        if {name.casefold() for name in sorted_names} != {name.casefold() for name in current}:
            missing = sorted({n.casefold() for n in current} - {n.casefold() for n in sorted_names})
            added = sorted({n.casefold() for n in sorted_names} - {n.casefold() for n in current})
            raise Mo2Error(f"LOOT changed the set of plugins; load order untouched. Missing: {missing[:10]} Added: {added[:10]}")
        report: dict[str, object] = {}
        if report_path.is_file():
            try:
                loaded = json.loads(report_path.read_text(encoding="utf-8"))
                report = loaded if isinstance(loaded, dict) else {}
            except json.JSONDecodeError:
                report = {}
        result.update(summarize_loot_report(report))
        moved = [name for index, name in enumerate(sorted_names) if index >= len(current) or current[index].casefold() != name.casefold()]
        result["order_changed"] = bool(moved)
        result["moved"] = len(moved)
        result["sorted_order"] = sorted_names
        if apply and moved:
            result["apply"] = apply_load_order(instance, profile, sorted_names)
            result["applied"] = True
    return result


# --- BSArch -----------------------------------------------------------------------------------


def bsarch(
    instance: Instance,
    profile: str | None,
    action: str,
    first: str,
    second: str | None = None,
    exe: str | None = None,
    fmt: str = "sse",
    compress: bool = True,
    multithread: bool = True,
    share: bool = False,
    filters: list[str] | None = None,
    capture_to: str | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    binary = find_tool(instance, exe, BSARCH_NAMES)
    if action == "pack":
        if not second:
            raise Mo2Error("bsarch pack needs SOURCE and ARCHIVE.")
        command = [str(binary), "pack", first, second, f"-{fmt}"]
        if compress:
            command.append("-z")
        command.append("-mt:yes" if multithread else "-mt:no")
        if share:
            command.append("-share:yes")
        if filters:
            command.append("-f:" + ",".join(filters))
    elif action == "unpack":
        if not second:
            raise Mo2Error("bsarch unpack needs ARCHIVE and DESTINATION.")
        command = [str(binary), "unpack", first, second, "-mt:yes" if multithread else "-mt:no"]
        if filters:
            command.append("-f:" + ",".join(filters))
    elif action == "list":
        command = [str(binary), first, "-list"]
    else:
        raise Mo2Error(f"Unknown BSArch action: {action}")
    result: dict[str, object] = {"action": action, "binary": str(binary), "command": command, "dry_run": dry_run}
    if dry_run:
        return result
    if action == "pack" and Path(str(second)).exists():
        raise Mo2Error(f"Archive already exists, not overwriting: {second}")
    before = snapshot_overwrite(instance) if capture_to else {}
    completed = subprocess.run(command, cwd=str(binary.parent), capture_output=True, text=True)
    result["returncode"] = completed.returncode
    if completed.returncode:
        raise Mo2Error(f"BSArch failed ({completed.returncode}): {(completed.stderr or completed.stdout).strip()[:500]}")
    if action == "list":
        result["files"] = [line.strip().replace("\\", "/") for line in completed.stdout.splitlines() if line.strip() and not line.startswith(("Archive", "BSArch", "Files:", "Total"))]
    if capture_to:
        result["capture"] = capture_new_files(instance, profile, capture_to, before)
    return result


# --- Synthesis and Pandora ---------------------------------------------------------------------

SYNTHESIS_NAMES = ("synthesis.cli", "synthesis")
PANDORA_NAMES = ("pandora behaviour engine+", "pandora behaviour engine", "pandora")


def _game_data(instance: Instance) -> Path:
    data = instance.game_path / "Data" if instance.game_path and (instance.game_path / "Data").is_dir() else instance.game_path
    if data is None:
        raise Mo2Error("gamePath not found in instance.")
    return data


def _run_generator(
    instance: Instance,
    profile: str | None,
    binary: Path,
    arguments: list[str],
    result: dict[str, object],
    output_mod: str,
    capture_to: str | None,
    usvfs_dir: str | None,
    destination: str | None,
) -> dict[str, object]:
    """Create the real output mod folder, run the tool in USVFS and optionally capture stray overwrite files."""
    ensure_output_mod(instance, profile, output_mod)
    before = snapshot_overwrite(instance)
    run = run_vfs(instance, profile, str(binary), arguments, usvfs_dir, destination or str(_game_data(instance)), direct_mods=True)
    result["returncode"] = run.get("returncode")
    result["ok"] = run.get("returncode") in (0, None)
    output_dir = instance.mods_dir / output_mod
    result["output_files"] = sum(1 for path in output_dir.rglob("*") if path.is_file() and path.name != "meta.ini") if output_dir.is_dir() else 0
    if capture_to:
        result["capture"] = capture_new_files(instance, profile, capture_to, before)
    return result


def synthesis_run(
    instance: Instance,
    profile: str | None,
    settings: str,
    output_mod: str = "Synthesis Output",
    exe: str | None = None,
    profile_identifier: str | None = None,
    data_folder: str | None = None,
    load_order: str | None = None,
    extra_data: str | None = None,
    target_runtime: str | None = None,
    capture_to: str | None = None,
    usvfs_dir: str | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    """``Synthesis.CLI run-pipeline`` with -o/-s/-p/-d/-l/-e/-t, run under USVFS.

    The output goes straight to the real folder ``mods/<output_mod>`` (-o), as
    recommended in Araclar-otomasyon.md; ``capture_to`` additionally collects
    anything the pipeline wrote to overwrite.
    """
    settings_path = Path(settings).expanduser()
    if not settings_path.is_file():
        raise Mo2Error(f"Synthesis settings file not found: {settings_path}")
    binary = find_tool(instance, exe, SYNTHESIS_NAMES)
    profile_path = instance.profile_path(profile)
    data = Path(data_folder).expanduser() if data_folder else _game_data(instance)
    order = Path(load_order).expanduser() if load_order else profile_path / "plugins.txt"
    output = instance.mods_dir / output_mod
    arguments = ["run-pipeline", "-o", str(output), "-s", str(settings_path.resolve())]
    if profile_identifier:
        arguments += ["-p", profile_identifier]
    arguments += ["-d", str(data), "-l", str(order)]
    if extra_data:
        arguments += ["-e", str(Path(extra_data).expanduser())]
    if target_runtime:
        arguments += ["-t", target_runtime]
    result: dict[str, object] = {"tool": "synthesis", "binary": str(binary), "arguments": arguments, "output_mod": output_mod, "output": str(output), "dry_run": dry_run, "virtualization": "usvfs"}
    if dry_run:
        return result
    return _run_generator(instance, profile, binary, arguments, result, output_mod, capture_to, usvfs_dir, str(data))


def pandora_run(
    instance: Instance,
    profile: str | None,
    output_mod: str = "Pandora Output",
    exe: str | None = None,
    tesv: str | None = None,
    auto_run: bool = True,
    auto_close: bool = True,
    capture_to: str | None = None,
    usvfs_dir: str | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    """Pandora Behaviour Engine+ with ``--tesv`` (game root, not Data), ``-o``, ``--auto_run``, ``--auto_close``."""
    binary = find_tool(instance, exe, PANDORA_NAMES)
    root = Path(tesv).expanduser() if tesv else instance.game_path
    if root is None:
        raise Mo2Error("gamePath not found in instance; pass --tesv.")
    output = instance.mods_dir / output_mod
    arguments = ["--tesv", str(root), "-o", str(output)]
    if auto_run:
        arguments.append("--auto_run")
    if auto_close:
        arguments.append("--auto_close")
    result: dict[str, object] = {"tool": "pandora", "binary": str(binary), "arguments": arguments, "output_mod": output_mod, "output": str(output), "dry_run": dry_run, "virtualization": "usvfs"}
    if auto_run:
        cache = binary.parent / "Pandora_Engine" / "ActiveMods.json"
        result["active_mods_cache"] = str(cache) if cache.is_file() else None
        if not cache.is_file():
            result["warning"] = "Pandora_Engine/ActiveMods.json not found: --auto_run needs one successful GUI run first."
    if dry_run:
        return result
    return _run_generator(instance, profile, binary, arguments, result, output_mod, capture_to, usvfs_dir, None)
