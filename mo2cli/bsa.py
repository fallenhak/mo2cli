"""Minimal reader for the directory of Bethesda ``BSA`` archives (versions 104 and 105).

Only names are read; nothing is extracted. Layout (UESP / libbsarch):
36 byte header, folder records (16 bytes in v104, 24 in v105), then per folder
a ``bzstring`` name followed by its file records (16 bytes), then the block of
null terminated file names.
"""

from __future__ import annotations

import struct
from pathlib import Path

from .workspace import Mo2Error

INCLUDE_DIRNAMES = 0x1
INCLUDE_FILENAMES = 0x2
SUPPORTED_VERSIONS = {104, 105}


def list_files(path: Path) -> list[str]:
    """Return archive paths lower-cased with ``/`` separators."""
    try:
        with path.open("rb") as source:
            header = source.read(36)
            if len(header) < 36 or header[:4] != b"BSA\0":
                raise Mo2Error(f"Not a BSA archive: {path}")
            _, version, folder_offset, flags, folder_count, file_count, _, file_names_length = struct.unpack_from("<4sIIIIIII", header)
            if version not in SUPPORTED_VERSIONS:
                raise Mo2Error(f"Unsupported BSA version {version}: {path}")
            if not flags & INCLUDE_FILENAMES or not flags & INCLUDE_DIRNAMES:
                raise Mo2Error(f"BSA has no file names stored: {path}")
            record_size = 16 if version == 104 else 24
            source.seek(folder_offset)
            counts = []
            for _ in range(folder_count):
                record = source.read(record_size)
                if len(record) < record_size:
                    raise Mo2Error(f"Truncated BSA folder table: {path}")
                counts.append(struct.unpack_from("<I", record, 8)[0])
            layout = []
            for count in counts:
                length = source.read(1)
                if not length:
                    raise Mo2Error(f"Truncated BSA folder block: {path}")
                name = source.read(length[0]).rstrip(b"\0").decode("cp1252", "replace")
                source.seek(count * 16, 1)
                layout.append((name, count))
            names_block = source.read(file_names_length)
    except OSError as error:
        raise Mo2Error(f"Could not read BSA {path}: {error}") from error
    names = names_block.split(b"\0")
    result = []
    index = 0
    for folder, count in layout:
        for _ in range(count):
            if index >= len(names) or index >= file_count:
                raise Mo2Error(f"BSA file name table is shorter than the file records: {path}")
            file_name = names[index].decode("cp1252", "replace")
            index += 1
            full = f"{folder}\\{file_name}" if folder else file_name
            result.append(full.replace("\\", "/").casefold())
    return result
