"""Wabbajack compile wrapper and a pre-flight check.

``wabbajack-cli compile -i INPUT -o OUTPUT`` flags come from the verified notes
in ``Araclar-otomasyon.md`` (``-i`` is a MO2 root or a ``*.compiler_settings``
file, ``-o`` is a folder; the file name comes from the settings). Whether the
Windows CLI accepts a full ``.wabbajack`` path for ``-o`` is not verified, so a
file path is split into folder and name and the result is renamed afterwards.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .downloads import audit_downloads
from .formats import ModList
from .metadata import ModMetadata
from .toolwrap import find_tool
from .workspace import Instance, Mo2Error

CLI_NAMES = ("wabbajack-cli", "wabbajack.cli")
NOMATCH_TAG = "WABBAJACK_NOMATCH_INCLUDE"
# Names that point at a tool output when a mod has no installationFile.
GENERATED_PATTERN = re.compile(r"(\boutput\b|pandora|nemesis|synthesis|dyndolod|xlodgen|texgen|bodyslide|reqtificator|bashed patch|smashed|parallaxgen|vramr|generated)", re.IGNORECASE)


def _norm(value: str) -> str:
    return value.replace("\\", "/").strip("/").casefold()


def read_settings(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise Mo2Error(f"Cannot read compiler settings {path}: {error}") from error
    if not isinstance(data, dict):
        raise Mo2Error(f"Compiler settings must be a JSON object: {path}")
    return data


def _settings_nomatch(settings: dict[str, object]) -> list[str]:
    values = settings.get("NoMatchInclude")
    return [_norm(str(item)) for item in values] if isinstance(values, list) else []


def preflight(instance: Instance, profile: str | None = None, settings: str | None = None, expect_version: str | None = None, stock_game: str | None = None) -> dict[str, object]:
    """Report what would make a Wabbajack compile fail or lose data."""
    problems: list[str] = []
    audit = audit_downloads(instance)
    if audit["archives_without_meta"]:
        problems.append(f"{len(audit['archives_without_meta'])} archive(s) in downloads have no .meta")
    if audit["archives_without_wabbajack_source"]:
        problems.append(f"{len(audit['archives_without_wabbajack_source'])} archive(s) have a .meta without a usable source (nexus ids, directURL or manualURL)")
    result: dict[str, object] = {
        "downloads": {
            "archives": audit["archives"],
            "without_meta": audit["archives_without_meta"],
            "without_source": audit["archives_without_wabbajack_source"],
        }
    }

    overwrite = sorted("/".join(path.relative_to(instance.overwrite_dir).parts) for path in instance.overwrite_dir.rglob("*") if path.is_file()) if instance.overwrite_dir.is_dir() else []
    result["overwrite"] = {"files": len(overwrite), "sample": overwrite[:20]}
    if overwrite:
        problems.append(f"overwrite is not empty ({len(overwrite)} file(s)); Wabbajack ignores it. Move them into a mod with 'tools ... --capture-to'")

    nomatch = _settings_nomatch(read_settings(Path(settings).expanduser())) if settings else []
    modlist = ModList.read(instance.profile_path(profile) / "modlist.txt")
    generated: list[dict[str, object]] = []
    for entry in modlist.entries:
        mod = instance.mods_dir / entry.name
        if not entry.enabled or not mod.is_dir() or entry.name.casefold().endswith("_separator"):
            continue
        values = ModMetadata.read(mod).values
        if str(values.get("installationFile") or "").strip() or not GENERATED_PATTERN.search(entry.name):
            continue
        notes = f"{values.get('notes', '')} {values.get('comments', '')}"
        tagged = NOMATCH_TAG in notes
        listed = any(item == f"mods/{_norm(entry.name)}" or item.startswith(f"mods/{_norm(entry.name)}/") for item in nomatch)
        generated.append({"mod": entry.name, "tagged": tagged, "in_settings": listed, "covered": tagged or listed})
    result["generated_mods"] = generated
    uncovered = [item["mod"] for item in generated if not item["covered"]]
    if uncovered:
        problems.append(f"generated output mod(s) not in No Match Include: {', '.join(map(str, uncovered))} (tag the mod notes with {NOMATCH_TAG} or add to NoMatchInclude)")

    directory = Path(stock_game).expanduser() if stock_game else instance.base / "Stock Game"
    if directory.is_dir():
        from . import stockgame

        try:
            verified = stockgame.verify(directory, expect_version)
        except Mo2Error as error:
            result["stock_game"] = {"directory": str(directory), "ok": False, "error": str(error)}
            problems.append(f"Stock Game: {error}")
        else:
            result["stock_game"] = {key: verified.get(key) for key in ("directory", "version", "expected_version", "ok")}
            if expect_version and not verified["ok"]:
                problems.append(f"Stock Game SkyrimSE.exe is {verified.get('version')}, expected {expect_version}")
    else:
        result["stock_game"] = {"directory": str(directory), "present": False}
        if stock_game or expect_version:
            problems.append(f"Stock Game folder not found: {directory}")
    result["problems"] = problems
    result["ok"] = not problems
    return result


def compile_modlist(
    instance: Instance,
    profile: str | None,
    output: str,
    settings: str | None = None,
    exe: str | None = None,
    expect_version: str | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, object]:
    binary = find_tool(instance, exe, CLI_NAMES)
    source = Path(settings).expanduser() if settings else instance.base
    if settings and not source.is_file():
        raise Mo2Error(f"Compiler settings not found: {source}")
    target = Path(output).expanduser()
    is_file = target.suffix.casefold() == ".wabbajack"
    folder = target.parent if is_file else target
    command = [str(binary), "compile", "-i", str(source), "-o", str(folder)]
    result: dict[str, object] = {"binary": str(binary), "command": command, "output": str(target), "dry_run": dry_run}
    check = preflight(instance, profile, settings, expect_version)
    result["preflight"] = check
    if dry_run:
        return result
    if not check["ok"] and not force:
        result["ok"] = False
        result["error"] = "Pre-flight check failed; fix the problems or use --force."
        return result
    folder.mkdir(parents=True, exist_ok=True)
    before = {path: path.stat().st_mtime_ns for path in folder.glob("*.wabbajack")}
    completed = subprocess.run(command, cwd=str(binary.parent), capture_output=True, text=True)
    result["returncode"] = completed.returncode
    result["log_tail"] = "\n".join((completed.stdout + completed.stderr).strip().splitlines()[-15:])
    result["ok"] = completed.returncode == 0
    if completed.returncode == 0:
        produced = [path for path in folder.glob("*.wabbajack") if before.get(path) != path.stat().st_mtime_ns]
        if produced:
            newest = max(produced, key=lambda path: path.stat().st_mtime)
            if is_file and newest.resolve() != target.resolve():
                if target.exists():
                    result["warning"] = f"{target.name} already exists; compiled file left as {newest.name}"
                    target = newest
                else:
                    newest.rename(target)
            elif not is_file:
                target = newest
            result["wabbajack_file"] = str(target)
        else:
            result["ok"] = False
            result["error"] = "wabbajack-cli exited 0 but no new .wabbajack file appeared."
    return result
