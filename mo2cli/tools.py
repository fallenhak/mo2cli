from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from .workspace import Instance, Mo2Error


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
