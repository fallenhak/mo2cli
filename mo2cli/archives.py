from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

try:
    import py7zr
except ImportError:  # pragma: no cover - exercised on installations without the backend
    py7zr = None

from .workspace import Mo2Error


ARCHIVE_SUFFIXES = (".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")


def archive_stem(path: Path) -> str:
    name = path.name
    lower = name.casefold()
    for suffix in sorted(ARCHIVE_SUFFIXES, key=len, reverse=True):
        if lower.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def _safe_member(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise Mo2Error(f"Unsafe path in archive: {name!r}")
    if len(path.parts[0]) >= 2 and path.parts[0][1] == ":":
        raise Mo2Error(f"Absolute Windows path in archive: {name!r}")
    return path


def _safe_extract_zip(path: Path, destination: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            relative = _safe_member(info.filename)
            target = destination.joinpath(*relative.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def _safe_extract_tar(path: Path, destination: Path) -> None:
    with tarfile.open(path) as archive:
        for member in archive.getmembers():
            relative = _safe_member(member.name)
            if member.issym() or member.islnk():
                raise Mo2Error(f"Archives containing symlinks are not supported: {member.name}")
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    continue
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)


def _seven_zip() -> str | None:
    for command in ("7z", "7zz", "7za"):
        if shutil.which(command):
            return command
    return None


def _tar_command() -> str | None:
    """Return the platform tar executable used as a RAR fallback.

    Recent Windows installations ship bsdtar, which can read RAR archives
    even when 7-Zip is not installed.  Keep this fallback after the native
    ZIP/tar and 7-Zip paths so existing behavior remains unchanged.
    """
    return shutil.which("tar")


def _unrar_command() -> str | None:
    """Locate UnRAR when Windows has WinRAR installed but it is not on PATH."""
    for command in ("unrar", "UnRAR", "unrar.exe", "UnRAR.exe"):
        found = shutil.which(command)
        if found:
            return found
    if os.name == "nt":
        for candidate in (
            Path(os.environ.get("ProgramFiles", "")) / "WinRAR" / "UnRAR.exe",
            Path(os.environ.get("ProgramFiles(x86)", "")) / "WinRAR" / "UnRAR.exe",
        ):
            if candidate.is_file():
                return str(candidate)
    return None


def _unrar_rar_members(path: Path) -> list[str]:
    command = _unrar_command()
    if not command:
        return []
    result = subprocess.run([command, "lb", "-c-", "-p-", "-y", str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode not in {0, 1}:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _safe_extract_rar_with_unrar(path: Path, destination: Path) -> bool:
    command = _unrar_command()
    if not command:
        return False
    members = _unrar_rar_members(path)
    if not members:
        return False
    for member in members:
        _safe_member(member)
    destination.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([command, "x", "-y", "-o+", str(path), str(destination)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise Mo2Error(f"RAR extraction failed: {result.stderr.strip() or result.stdout.strip()}")
    return True


def _tar_rar_members(path: Path) -> list[str]:
    command = _tar_command()
    if not command:
        return []
    result = subprocess.run([command, "-tf", str(path)], capture_output=True, text=True)
    if result.returncode:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def _safe_extract_rar_with_tar(path: Path, destination: Path) -> bool:
    members = _tar_rar_members(path)
    if not members:
        return False
    for member in members:
        _safe_member(member)
    destination.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [shutil.which("tar") or "tar", "-xf", str(path), "-C", str(destination)],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise Mo2Error(f"RAR extraction failed: {result.stderr.strip() or result.stdout.strip()}")
    return True


def _safe_extract_py7zr(path: Path, destination: Path) -> None:
    if py7zr is None:
        raise Mo2Error("py7zr backend not found.")
    try:
        with py7zr.SevenZipFile(path, mode="r") as archive:
            for name in archive.getnames():
                _safe_member(str(name))
            destination.mkdir(parents=True, exist_ok=True)
            archive.extractall(path=destination)
    except Mo2Error:
        raise
    except Exception as error:
        raise Mo2Error(f"7z archive extraction failed: {error}") from error


def extract_archive(path: Path, destination: Path) -> None:
    suffix = path.name.casefold()
    if suffix.endswith(".zip"):
        _safe_extract_zip(path, destination)
        return
    if suffix.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
        _safe_extract_tar(path, destination)
        return
    if suffix.endswith(".rar"):
        if _safe_extract_rar_with_unrar(path, destination):
            return
        if _safe_extract_rar_with_tar(path, destination):
            return
    command = _seven_zip()
    if not command and py7zr is not None:
        _safe_extract_py7zr(path, destination)
        return
    if not command:
        raise Mo2Error("7z/7zz/7za executable required for this archive.")
    # 7-Zip performs extraction itself; validate every listed member first.
    for item in list_archive(path):
        _safe_member(str(item["name"]))
    destination.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([command, "x", "-y", f"-o{destination}", str(path)], capture_output=True, text=True)
    if result.returncode:
        raise Mo2Error(f"7z extraction failed: {result.stderr.strip() or result.stdout.strip()}")


def list_archive(path: Path) -> list[dict[str, object]]:
    suffix = path.name.casefold()
    if suffix.endswith(".zip"):
        with zipfile.ZipFile(path) as archive:
            return [{"name": info.filename, "size": info.file_size, "compressed": info.compress_size, "directory": info.is_dir()} for info in archive.infolist()]
    if suffix.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
        with tarfile.open(path) as archive:
            return [{"name": info.name, "size": info.size, "compressed": None, "directory": info.isdir()} for info in archive.getmembers()]
    if suffix.endswith(".rar"):
        members = _unrar_rar_members(path)
        if members:
            return [{"name": name, "size": 0, "compressed": None, "directory": name.endswith(("/", "\\"))} for name in members]
        members = _tar_rar_members(path)
        if members:
            return [{"name": name, "size": 0, "compressed": None, "directory": name.endswith(("/", "\\"))} for name in members]
    command = _seven_zip()
    if not command and py7zr is not None:
        try:
            with py7zr.SevenZipFile(path, mode="r") as archive:
                return [{"name": str(item.filename), "size": int(getattr(item, "uncompressed", 0) or 0), "compressed": int(getattr(item, "compressed", 0) or 0), "directory": bool(getattr(item, "is_directory", False))} for item in archive.list()]
        except Exception as error:
            raise Mo2Error(f"7z listing failed: {error}") from error
    if not command:
        raise Mo2Error("7z/7zz/7za required for 7z/RAR listing.")
    result = subprocess.run([command, "l", "-slt", str(path)], capture_output=True, text=True)
    if result.returncode:
        raise Mo2Error(f"7z listing failed: {result.stderr.strip() or result.stdout.strip()}")
    records: list[dict[str, object]] = []
    current: dict[str, str] = {}
    for line in result.stdout.splitlines() + [""]:
        if not line.strip():
            if current.get("Path") and current.get("Path") not in {str(path), ""}:
                records.append({"name": current["Path"], "size": int(current.get("Size", "0") or 0), "compressed": int(current.get("Packed Size", "0") or 0), "directory": current.get("Folder") == "+"})
            current = {}
            continue
        if " = " in line:
            key, value = line.split(" = ", 1)
            current[key] = value
    return records


def sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class ExtractedArchive:
    root: Path
    temp_dir: tempfile.TemporaryDirectory | None = None

    def close(self) -> None:
        if self.temp_dir is not None:
            self.temp_dir.cleanup()


def extract_to_temp(path: Path) -> ExtractedArchive:
    temp_dir = tempfile.TemporaryDirectory(prefix="mo2cli-archive-")
    destination = Path(temp_dir.name)
    try:
        extract_archive(path, destination)
        return ExtractedArchive(root=destination, temp_dir=temp_dir)
    except Exception:
        temp_dir.cleanup()
        raise
