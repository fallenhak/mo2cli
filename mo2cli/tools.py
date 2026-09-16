from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from .metadata import IniDocument
from .workspace import Instance, Mo2Error


def register_executable(
    instance: Instance,
    title: str,
    binary: str,
    working_directory: str | None = None,
    arguments: str = "",
    replace: bool = False,
) -> dict[str, object]:
    """Register a custom executable in ModOrganizer.ini."""
    clean_title = title.strip()
    if not clean_title:
        raise Mo2Error("Executable title cannot be empty.")
    binary_path = Path(binary).expanduser().resolve()
    if not binary_path.is_file():
        raise Mo2Error(f"Executable file not found: {binary_path}")
    working_path = Path(working_directory).expanduser().resolve() if working_directory else binary_path.parent
    if not working_path.is_dir():
        raise Mo2Error(f"Working directory not found: {working_path}")

    document = IniDocument.read(instance.ini)
    existing = next(
        (item for item in instance.executables() if str(item.get("title", "")).casefold() == clean_title.casefold()),
        None,
    )
    if existing is not None and not replace:
        raise Mo2Error(f"MO2 executable already exists: {clean_title}. Use --replace to update it.")
    indexes = [int(item["index"]) for item in instance.executables()]
    index = int(existing["index"]) if existing is not None else (max(indexes, default=0) + 1)
    # QSettings treats backslashes in INI values as escape characters. Use
    # forward slashes so MO2 can safely load and save Windows tool paths.
    fields: dict[str, object] = {
        "title": clean_title,
        "binary": binary_path.as_posix(),
        "workingDirectory": working_path.as_posix(),
        "arguments": arguments,
        "hide": False,
        "ownicon": True,
        "steamAppID": "",
        "toolbar": False,
    }
    for field, value in fields.items():
        document.set(f"{index}\\{field}", value, section="customExecutables")
    document.write(instance.ini)
    return fields | {"index": index, "replaced": existing is not None}


def remove_executable(instance: Instance, target: str) -> dict[str, object]:
    """Remove one MO2 custom executable by numeric index or unique title."""
    candidates = instance.executables()
    clean_target = target.strip()
    if not clean_target:
        raise Mo2Error("Executable target cannot be empty.")

    if clean_target.isdigit():
        matches = [item for item in candidates if int(item["index"]) == int(clean_target)]
    else:
        wanted = clean_target.casefold()
        matches = [item for item in candidates if str(item.get("title", "")).casefold() == wanted]
    if not matches:
        raise Mo2Error(f"MO2 executable not found: {clean_target}")
    if len(matches) > 1:
        indexes = ", ".join(str(item["index"]) for item in matches)
        raise Mo2Error(f"MO2 executable title is ambiguous: {clean_target}. Use one of these indexes: {indexes}")

    executable = matches[0]
    index = int(executable["index"])
    document = IniDocument.read(instance.ini)
    values = document.as_dict("customExecutables")
    keys = [key for key in values if key.partition("\\")[0] == str(index)]
    for key in keys:
        document.remove(key, section="customExecutables")
    document.write(instance.ini)
    return {"index": index, "title": executable.get("title", ""), "removed_fields": len(keys)}


def find_executable(instance: Instance, title: str) -> dict[str, object]:
    wanted = title.casefold()
    candidates = instance.executables()
    match = next((item for item in candidates if str(item.get("title", "")).casefold() == wanted), None)
    if match is None:
        match = next((item for item in candidates if Path(str(item.get("binary", ""))).stem.casefold() == wanted), None)
    if match is None:
        raise Mo2Error(f"MO2 executable not found: {title}")
    binary = Path(str(match.get("binary", ""))).expanduser()
    if not binary.is_file():
        raise Mo2Error(f"Executable file not found: {binary}")
    match = dict(match)
    match["binary"] = str(binary)
    return match


def run_executable(instance: Instance, title: str, extra_args: list[str] | None = None, wait: bool = False) -> dict[str, object]:
    executable = find_executable(instance, title)
    binary = str(executable["binary"])
    configured = str(executable.get("arguments", ""))
    arguments = shlex.split(configured, posix=False) if configured else []
    command = [binary, *arguments, *(extra_args or [])]
    cwd = str(executable.get("workingDirectory") or Path(binary).parent)
    environment = os.environ.copy()
    if instance.selected_profile:
        environment["MO2_PROFILE"] = instance.selected_profile
    process = subprocess.Popen(command, cwd=cwd, env=environment)
    result: dict[str, object] = {"title": executable.get("title", title), "binary": binary, "pid": process.pid, "waited": wait, "virtualization": "direct"}
    if wait:
        result["returncode"] = process.wait()
    return result
