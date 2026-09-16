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
MAX_ARCHIVE_FILES = 100_000
MAX_ARCHIVE_BYTES = 64 * 1024**3
MAX_COMPRESSION_RATIO = 10_000


def _solid_member_compressed_size(value: object) -> int | None:
    """Return a meaningful packed size from a solid-archive listing.

    7z reports a packed size only on the first member of a solid block and
    reports zero for the remaining members. A zero therefore means "shared or
    unknown", not that a non-empty member compressed to zero bytes.
    """
    size = int(value or 0)
    return size if size > 0 else None


def _validate_expansion(records: list[dict[str, object]], destination: Path) -> None:
    files = [item for item in records if not item.get("directory")]
    if len(files) > MAX_ARCHIVE_FILES:
        raise Mo2Error(f"Archive contains too many files ({len(files):,}; limit {MAX_ARCHIVE_FILES:,}).")
    total = 0
    for item in files:
        size = max(0, int(item.get("size") or 0))
        compressed_value = item.get("compressed")
        compressed = max(0, int(compressed_value or 0)) if compressed_value is not None else None
        total += size
        if size > MAX_ARCHIVE_BYTES:
            raise Mo2Error(f"Archive member is too large to extract safely: {item.get('name')}")
        if compressed is not None and size > 1024**2 and (compressed == 0 or size / compressed > MAX_COMPRESSION_RATIO):
            raise Mo2Error(f"Archive member has a suspicious compression ratio: {item.get('name')}")
    if total > MAX_ARCHIVE_BYTES:
        raise Mo2Error(f"Archive expands to {total:,} bytes; safety limit is {MAX_ARCHIVE_BYTES:,} bytes.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(destination.parent).free
    if total and total > free:
        raise Mo2Error(f"Archive requires {total:,} bytes but only {free:,} bytes are free.")


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
    reserved = {"con", "prn", "aux", "nul", *(f"com{index}" for index in range(1, 10)), *(f"lpt{index}" for index in range(1, 10))}
    for part in path.parts:
        if ":" in part or part != part.rstrip(" .") or part.split(".", 1)[0].casefold() in reserved:
            raise Mo2Error(f"Unsafe Windows path in archive: {name!r}")
    return path


def _archive_item_is_link(item: object) -> bool:
    for attribute in ("is_symlink", "is_hardlink", "is_junction"):
        value = getattr(item, attribute, False)
        if callable(value):
            value = value()
        if value:
            return True
    return bool(getattr(item, "linkname", None) or getattr(item, "link_name", None))


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
    if os.name == "nt":
        prog_files = os.environ.get("ProgramFiles", "C:\\Program Files")
        prog_files_x86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            Path(prog_files) / "7-Zip" / "7z.exe",
            Path(prog_files_x86) / "7-Zip" / "7z.exe",
            Path(prog_files) / "Black Tree Gaming Ltd" / "Vortex" / "resources" / "app.asar.unpacked" / "node_modules" / "7z-bin" / "win32" / "7z.exe",
            Path(prog_files) / "NVIDIA Corporation" / "NVIDIA App" / "7z.exe",
            Path(local_appdata) / "Programs" / "7-Zip" / "7z.exe",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
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
    result = subprocess.run([command, "x", "-y", "-o+", "-ol-", str(path), str(destination)], capture_output=True, text=True, encoding="utf-8", errors="replace")
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
    # ``tar -tf`` does not expose link targets in a reliably parseable,
    # cross-platform format. Keep it as a listing fallback only: extracting an
    # untrusted RAR without link metadata could write outside the destination.
    return False


def _safe_extract_py7zr(path: Path, destination: Path) -> None:
    if py7zr is None:
        raise Mo2Error("py7zr backend not found.")
    try:
        with py7zr.SevenZipFile(path, mode="r") as archive:
            for item in archive.list():
                _safe_member(str(item.filename))
                if _archive_item_is_link(item):
                    raise Mo2Error(f"Archives containing links are not supported: {item.filename}")
            destination.mkdir(parents=True, exist_ok=True)
            archive.extractall(path=destination)
    except Mo2Error:
        raise
    except Exception as error:
        raise Mo2Error(f"7z archive extraction failed: {error}") from error


def extract_archive(path: Path, destination: Path) -> None:
    suffix = path.name.casefold()
    records = list_archive(path)
    _validate_expansion(records, destination)
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
    for item in records:
        _safe_member(str(item["name"]))
        if item.get("symbolic_link") or item.get("hard_link"):
            raise Mo2Error(f"Archives containing links are not supported: {item['name']}")
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
                return [{"name": str(item.filename), "size": int(getattr(item, "uncompressed", 0) or 0), "compressed": _solid_member_compressed_size(getattr(item, "compressed", 0)), "directory": bool(getattr(item, "is_directory", False))} for item in archive.list()]
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
                records.append({
                    "name": current["Path"],
                    "size": int(current.get("Size", "0") or 0),
                    "compressed": _solid_member_compressed_size(current.get("Packed Size", "0")),
                    "directory": current.get("Folder") == "+",
                    "symbolic_link": current.get("Symbolic Link"),
                    "hard_link": current.get("Hard Link"),
                })
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
