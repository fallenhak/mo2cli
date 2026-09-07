from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path, PurePosixPath

from .workspace import Instance, Mo2Error, _safe_name


def _safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise Mo2Error(f"Unsafe path in profile archive: {name}")
    return path


def export_profile(instance: Instance, profile: str | None, destination: str | Path) -> dict[str, object]:
    profile_path = instance.profile_path(profile)
    output = Path(destination).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"format": "mo2cli-profile", "version": 1, "profile": profile_path.name}
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for path in profile_path.rglob("*"):
            if path.is_file():
                archive.write(path, Path("profile") / path.relative_to(profile_path))
    return {"archive": str(output), "profile": profile_path.name, "files": sum(1 for path in profile_path.rglob("*") if path.is_file())}


def import_profile(instance: Instance, archive_path: str | Path, name: str | None = None, replace: bool = False) -> dict[str, object]:
    source = Path(archive_path).expanduser().resolve()
    if not source.is_file():
        raise Mo2Error(f"Profile archive not found: {source}")
    with zipfile.ZipFile(source) as archive:
        try:
            manifest = json.loads(archive.read("manifest.json"))
        except (KeyError, json.JSONDecodeError) as error:
            raise Mo2Error(f"Invalid profile archive: {error}") from error
        if manifest.get("format") != "mo2cli-profile":
            raise Mo2Error("This file is not a mo2cli profile archive.")
        profile_name = name or manifest.get("profile")
        if not isinstance(profile_name, str):
            raise Mo2Error("Profile name not found in archive manifest.")
        _safe_name(profile_name)
        destination = instance.profiles_dir / profile_name
        if destination.exists():
            if not replace:
                raise Mo2Error(f"Profile already exists: {profile_name}; use --replace to overwrite.")
            shutil.rmtree(destination)
        destination.mkdir(parents=True, exist_ok=False)
        try:
            for member in archive.infolist():
                if member.filename == "manifest.json" or member.is_dir():
                    continue
                relative = _safe_member(member.filename)
                if len(relative.parts) < 2 or relative.parts[0].casefold() != "profile":
                    raise Mo2Error(f"Unexpected file in profile archive: {member.filename}")
                target = destination.joinpath(*relative.parts[1:])
                if not target.resolve().is_relative_to(destination.resolve()):
                    raise Mo2Error(f"Unsafe target in profile archive: {member.filename}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source_file, target.open("wb") as output:
                    shutil.copyfileobj(source_file, output)
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise
    return {"profile": profile_name, "path": str(destination), "archive": str(source)}
