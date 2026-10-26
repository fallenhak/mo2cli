"""Build and verify a Wabbajack style ``Stock Game`` folder.

``create`` only ever reads from the source (a Steam install, which must stay
untouched) and writes into the destination. Copy rule:

* Everything at the top level of the game folder is copied, except files and
  folders that mods put there (SKSE loader and DLLs, ENB/ReShade files,
  ``d3d11.dll``/``dxgi.dll`` proxies, logs, ``Data`` mods).
* ``Data`` is not copied as a whole. Only the vanilla content that sits
  directly in ``Data`` is: the official ``.esm`` files, the official
  ``Skyrim - *.bsa``/``Dawnguard``/``HearthFires``/``Dragonborn``/``Update``
  archives, Creation Club files (``cc*.esm/.esl/.esp/.bsa/.cdb``), the
  Anniversary ``_ResourcePack`` files and the ``Data\\Video`` folder.
  Everything else in ``Data`` (loose mod files, SKSE plugins, mod ``.esp``)
  is reported as skipped and not copied.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import shutil
import struct
from pathlib import Path

from .journal import record
from .workspace import Instance, Mo2Error

EXE_NAME = "SkyrimSE.exe"

# Top level game folder names that belong to mods, not to the vanilla install.
ROOT_EXCLUDE = (
    "skse64_*", "skse_*", "sksevr_*", "d3d11.dll", "d3d12.dll", "dxgi.dll", "d3dcompiler_46e.dll", "dinput8.dll",
    "enb*", "reshade*", "*.log", "crashlogs", "skse", "enbseries", "enbcache", "data", ".mo2cli*",
)

DATA_FILE_PATTERNS = (
    "skyrim.esm", "update.esm", "dawnguard.esm", "hearthfires.esm", "dragonborn.esm",
    "skyrim - *.bsa", "update.bsa", "dawnguard.bsa", "hearthfires.bsa", "dragonborn.bsa",
    "cc*.esm", "cc*.esl", "cc*.esp", "cc*.bsa", "cc*.cdb",
    "_resourcepack.esl", "_resourcepack.bsa", "_resourcepack.esm",
)
DATA_DIRS = ("video",)


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    folded = name.casefold()
    return any(fnmatch.fnmatchcase(folded, pattern) for pattern in patterns)


def plan_copy(source: Path) -> tuple[list[tuple[Path, Path]], list[str]]:
    """Return ``[(source file, path relative to the game folder)]`` and the skipped relative names."""
    copies: list[tuple[Path, Path]] = []
    skipped: list[str] = []
    for entry in sorted(source.iterdir(), key=lambda item: item.name.casefold()):
        if entry.name.casefold() == "data":
            continue
        if _matches(entry.name, ROOT_EXCLUDE):
            skipped.append(entry.name)
            continue
        if entry.is_dir():
            for item in sorted(entry.rglob("*")):
                if item.is_file():
                    copies.append((item, item.relative_to(source)))
        elif entry.is_file():
            copies.append((entry, Path(entry.name)))
    data = next((item for item in source.iterdir() if item.name.casefold() == "data" and item.is_dir()), None)
    if data is not None:
        for entry in sorted(data.iterdir(), key=lambda item: item.name.casefold()):
            relative = Path(data.name) / entry.name
            if entry.is_dir() and entry.name.casefold() in DATA_DIRS:
                for item in sorted(entry.rglob("*")):
                    if item.is_file():
                        copies.append((item, Path(data.name) / item.relative_to(data)))
            elif entry.is_file() and _matches(entry.name, DATA_FILE_PATTERNS):
                copies.append((entry, relative))
            else:
                skipped.append(str(relative).replace("\\", "/"))
    return copies, skipped


# --- file version -----------------------------------------------------------------


def _rva_to_offset(sections: list[tuple[int, int, int, int]], rva: int) -> int | None:
    for virtual, virtual_size, raw_pointer, raw_size in sections:
        if virtual <= rva < virtual + max(virtual_size, raw_size):
            return raw_pointer + rva - virtual
    return None


def pe_file_version(path: Path) -> str | None:
    """Read ``VS_FIXEDFILEINFO`` from the PE version resource without any Windows API."""
    data = path.read_bytes()
    if data[:2] != b"MZ" or len(data) < 0x40:
        return None
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe : pe + 4] != b"PE\0\0":
        return None
    section_count, = struct.unpack_from("<H", data, pe + 6)
    optional_size, = struct.unpack_from("<H", data, pe + 20)
    optional = pe + 24
    magic, = struct.unpack_from("<H", data, optional)
    directories = optional + (112 if magic == 0x20B else 96)
    resource_rva, resource_size = struct.unpack_from("<II", data, directories + 2 * 8)
    if not resource_rva:
        return None
    table = optional + optional_size
    sections = []
    for index in range(section_count):
        virtual_size, virtual, raw_size, raw_pointer = struct.unpack_from("<IIII", data, table + index * 40 + 8)
        sections.append((virtual, virtual_size, raw_pointer, raw_size))
    base = _rva_to_offset(sections, resource_rva)
    if base is None:
        return None

    def entries(offset: int) -> list[tuple[int, int]]:
        named, ids = struct.unpack_from("<HH", data, offset + 12)
        result = []
        for index in range(named + ids):
            name, target = struct.unpack_from("<II", data, offset + 16 + index * 8)
            result.append((name, target))
        return result

    try:
        version_dir = next((target for name, target in entries(base) if name == 16), None)
        if version_dir is None or not version_dir & 0x80000000:
            return None
        language_dir = entries(base + (version_dir & 0x7FFFFFFF))
        if not language_dir:
            return None
        # first name -> first language -> data entry
        level = language_dir[0][1]
        if level & 0x80000000:
            inner = entries(base + (level & 0x7FFFFFFF))
            if not inner:
                return None
            level = inner[0][1]
        rva, size = struct.unpack_from("<II", data, base + level)
        offset = _rva_to_offset(sections, rva)
        if offset is None:
            return None
        block = data[offset : offset + size]
        position = block.find(struct.pack("<I", 0xFEEF04BD))
        if position < 0:
            return None
        ms, ls = struct.unpack_from("<II", block, position + 8)
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except (struct.error, IndexError):
        return None


def _windows_file_version(path: Path) -> str | None:
    import ctypes
    from ctypes import wintypes

    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    version.GetFileVersionInfoW.restype = wintypes.BOOL
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    version.VerQueryValueW.restype = wintypes.BOOL
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        return None
    pointer = ctypes.c_void_p()
    length = wintypes.UINT()
    if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)) or length.value < 52:
        return None
    fixed = ctypes.string_at(pointer.value, length.value)
    ms, ls = struct.unpack_from("<II", fixed, 8)
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def file_version(path: Path) -> str | None:
    """File version of an executable (Windows version API first, pure Python PE reader otherwise)."""
    import os

    if os.name == "nt":
        try:
            found = _windows_file_version(path)
            if found:
                return found
        except (OSError, AttributeError, ValueError):
            pass
    return pe_file_version(path)


# --- hashing -------------------------------------------------------------------------

_P1, _P2, _P3, _P4, _P5 = 11400714785074694791, 14029467366897019727, 1609587929392839161, 9650029242287828579, 2870177450012600261
_MASK = (1 << 64) - 1


def _rotl(value: int, count: int) -> int:
    return ((value << count) | (value >> (64 - count))) & _MASK


def _round(acc: int, lane: int) -> int:
    return (_rotl((acc + lane * _P2) & _MASK, 31) * _P1) & _MASK


def _merge(acc: int, lane: int) -> int:
    return ((acc ^ _round(0, lane)) * _P1 + _P4) & _MASK


class XXH64:
    """Streaming XXH64 (seed 0); used when the optional ``xxhash`` package is not installed."""

    def __init__(self) -> None:
        self.v = [(_P1 + _P2) & _MASK, _P2, 0, (-_P1) & _MASK]
        self.buffer = b""
        self.length = 0

    def update(self, data: bytes) -> None:
        self.length += len(data)
        data = self.buffer + data
        usable = len(data) - len(data) % 32
        v1, v2, v3, v4 = self.v
        unpack = struct.Struct("<4Q").unpack_from
        for offset in range(0, usable, 32):
            a, b, c, d = unpack(data, offset)
            v1 = _round(v1, a)
            v2 = _round(v2, b)
            v3 = _round(v3, c)
            v4 = _round(v4, d)
        self.v = [v1, v2, v3, v4]
        self.buffer = data[usable:]

    def intdigest(self) -> int:
        v1, v2, v3, v4 = self.v
        if self.length >= 32:
            h = (_rotl(v1, 1) + _rotl(v2, 7) + _rotl(v3, 12) + _rotl(v4, 18)) & _MASK
            for lane in (v1, v2, v3, v4):
                h = _merge(h, lane)
        else:
            h = _P5
        h = (h + self.length) & _MASK
        data = self.buffer
        offset = 0
        while offset + 8 <= len(data):
            h ^= _round(0, struct.unpack_from("<Q", data, offset)[0])
            h = (_rotl(h, 27) * _P1 + _P4) & _MASK
            offset += 8
        if offset + 4 <= len(data):
            h ^= (struct.unpack_from("<I", data, offset)[0] * _P1) & _MASK
            h = (_rotl(h, 23) * _P2 + _P3) & _MASK
            offset += 4
        while offset < len(data):
            h ^= (data[offset] * _P5) & _MASK
            h = (_rotl(h, 11) * _P1) & _MASK
            offset += 1
        h ^= h >> 33
        h = (h * _P2) & _MASK
        h ^= h >> 29
        h = (h * _P3) & _MASK
        h ^= h >> 32
        return h


def hash_file(path: Path) -> dict[str, object]:
    """xxHash64 (hex plus the base64 form Wabbajack indexes use) and SHA-256 of a file."""
    try:
        import xxhash  # type: ignore[import-not-found]

        fast = xxhash.xxh64()
        engine = "xxhash"
    except ImportError:
        fast = XXH64()
        engine = "pure-python"
    sha = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            fast.update(chunk)
            sha.update(chunk)
    value = fast.intdigest()
    return {
        "xxhash64": f"{value:016x}",
        "xxhash64_base64": base64.b64encode(value.to_bytes(8, "little")).decode("ascii"),
        "xxhash_engine": engine,
        "sha256": sha.hexdigest(),
    }


# --- commands ------------------------------------------------------------------------


def _inspect(directory: Path) -> dict[str, object]:
    exe = directory / EXE_NAME
    if not exe.is_file():
        raise Mo2Error(f"{EXE_NAME} not found in {directory}")
    result: dict[str, object] = {"directory": str(directory), "exe": str(exe), "version": file_version(exe), "size": exe.stat().st_size}
    result.update(hash_file(exe))
    return result


def verify(directory: Path, expect_version: str | None = None) -> dict[str, object]:
    result = _inspect(directory)
    if expect_version is not None:
        result["expected_version"] = expect_version
        result["ok"] = result["version"] == expect_version
    else:
        result["ok"] = result["version"] is not None
    return result


def create(
    instance: Instance,
    source: str | Path,
    destination: str | Path | None = None,
    expect_version: str | None = None,
    allow_mismatch: bool = False,
    dry_run: bool = False,
) -> dict[str, object]:
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_dir() or not (source_path / EXE_NAME).is_file():
        raise Mo2Error(f"--from must be a game folder containing {EXE_NAME}: {source_path}")
    target = Path(destination).expanduser() if destination else instance.base / "Stock Game"
    target = target.resolve()
    if target == source_path or target.is_relative_to(source_path) or source_path.is_relative_to(target):
        raise Mo2Error("--to and --from must be separate folders; the source install is never modified.")
    version = file_version(source_path / EXE_NAME)
    result: dict[str, object] = {"from": str(source_path), "to": str(target), "version": version, "dry_run": dry_run}
    if expect_version is not None:
        result["expected_version"] = expect_version
        if version != expect_version:
            if not allow_mismatch:
                raise Mo2Error(f"{EXE_NAME} is version {version or 'unknown'}, expected {expect_version}. Use --allow-mismatch to copy anyway.")
            result["version_mismatch_allowed"] = True
    copies, skipped = plan_copy(source_path)
    todo = []
    unchanged = 0
    for item, relative in copies:
        existing = target / relative
        if existing.is_file() and existing.stat().st_size == item.stat().st_size:
            unchanged += 1
        else:
            todo.append((item, relative))
    result.update({
        "files": len(copies),
        "bytes": sum(item.stat().st_size for item, _ in todo),
        "to_copy": len(todo),
        "already_present": unchanged,
        "skipped": skipped,
    })
    if dry_run:
        return result
    created_root = not target.exists()
    target.mkdir(parents=True, exist_ok=True)
    for item, relative in todo:
        destination_file = target / relative
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination_file)
    result["verify"] = verify(target, expect_version)
    inside = target.is_relative_to(instance.base.resolve())
    entry = record(instance, "stockgame_create", reversible=created_root and inside, destination=str(target), source=str(source_path), files=len(todo))
    result["journal_id"] = entry["id"]
    return result
