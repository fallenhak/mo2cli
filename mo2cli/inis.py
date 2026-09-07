from __future__ import annotations

import difflib
from pathlib import Path

from .metadata import IniDocument
from .workspace import Instance, Mo2Error, _safe_relative_path


def profile_ini_path(instance: Instance, profile: str | None, filename: str) -> Path:
    parts = _safe_relative_path(filename)
    if not filename.casefold().endswith((".ini", ".cfg")):
        raise Mo2Error("INI file must end with .ini or .cfg extension.")
    profile_path = instance.profile_path(profile)
    target = profile_path.joinpath(*parts)
    if not target.resolve().is_relative_to(profile_path.resolve()):
        raise Mo2Error("INI path escapes profile directory.")
    return target


def list_inis(instance: Instance, profile: str | None) -> list[dict[str, object]]:
    profile_path = instance.profile_path(profile)
    return [{"name": str(path.relative_to(profile_path)), "path": str(path), "bytes": path.stat().st_size} for path in sorted(profile_path.rglob("*.ini"), key=lambda item: str(item).casefold()) if path.is_file()]


def get_value(instance: Instance, profile: str | None, filename: str, section: str | None, key: str):
    path = profile_ini_path(instance, profile, filename)
    return IniDocument.read(path).get(key, None, section)


def set_value(instance: Instance, profile: str | None, filename: str, section: str | None, key: str, value: str) -> Path:
    path = profile_ini_path(instance, profile, filename)
    document = IniDocument.read(path)
    document.set(key, value, section)
    document.write(path)
    return path


def diff_game_ini(instance: Instance, profile: str | None, filename: str) -> str:
    profile_file = profile_ini_path(instance, profile, filename)
    if instance.game_path is None:
        raise Mo2Error("gamePath not found in instance.")
    game_file = instance.game_path / Path(filename)
    profile_lines = profile_file.read_text(encoding="utf-8-sig", errors="replace").splitlines() if profile_file.exists() else []
    game_lines = game_file.read_text(encoding="utf-8-sig", errors="replace").splitlines() if game_file.exists() else []
    return "\n".join(difflib.unified_diff(game_lines, profile_lines, fromfile=str(game_file), tofile=str(profile_file), lineterm=""))
