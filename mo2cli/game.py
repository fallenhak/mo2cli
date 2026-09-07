from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .workspace import Instance, Mo2Error


KNOWN_BINARIES = {
    "skyrim": ("SkyrimSE.exe", "TESV.exe"),
    "fallout 4": ("Fallout4.exe",),
    "fallout 3": ("Fallout3.exe",),
    "fallout: new vegas": ("FalloutNV.exe",),
    "fallout new vegas": ("FalloutNV.exe",),
    "oblivion": ("Oblivion.exe",),
    "morrowind": ("Morrowind.exe",),
    "starfield": ("Starfield.exe",),
    "cyberpunk 2077": ("Cyberpunk2077.exe",),
}


def find_game_binary(instance: Instance, explicit: str | None = None) -> Path:
    if instance.game_path is None or not instance.game_path.is_dir():
        raise Mo2Error("Valid gamePath not found in instance.")
    if explicit:
        binary = Path(explicit)
        if not binary.is_absolute():
            binary = instance.game_path / binary
        if binary.is_file():
            return binary.resolve()
        raise Mo2Error(f"Game binary not found: {binary}")
    candidates = []
    for key, names in KNOWN_BINARIES.items():
        if key in instance.game_name.casefold():
            candidates.extend(instance.game_path / name for name in names)
    candidates.extend(instance.game_path / name for name in ("game.exe", "Game.exe"))
    existing = next((path for path in candidates if path.is_file()), None)
    if existing:
        return existing.resolve()
    exes = list(instance.game_path.glob("*.exe"))
    if len(exes) == 1:
        return exes[0].resolve()
    raise Mo2Error("Game binary could not be located automatically; specify --binary.")


def run_game(instance: Instance, binary: str | None = None, extra_args: list[str] | None = None, wait: bool = False) -> dict[str, object]:
    executable = find_game_binary(instance, binary)
    process = subprocess.Popen([str(executable), *(extra_args or [])], cwd=str(executable.parent), env=os.environ.copy())
    result: dict[str, object] = {"binary": str(executable), "pid": process.pid, "waited": wait, "virtualization": "direct"}
    if wait:
        result["returncode"] = process.wait()
    return result
