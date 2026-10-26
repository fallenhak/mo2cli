"""Minimal QSettings INI value codec as used by Mod Organizer 2.

MO2 stores paths and profile names as ``QByteArray`` values
(``settings.cpp``: ``GameSettings::setDirectory`` and
``setSelectedProfileName`` call ``set(..., QByteArray)``). QSettings writes
those as ``@ByteArray(...)`` and doubles every backslash, so a game path reads
``gamePath=@ByteArray(C:\\Games\\Skyrim Special Edition)`` on disk.
"""

from __future__ import annotations

import re

_HEX = re.compile(r"[0-9a-fA-F]{1,4}")


def decode(value: str) -> str:
    """Decode one raw INI value (``@ByteArray(...)``, quotes and ``\\`` escapes)."""
    value = value.strip()
    is_bytes = value.startswith("@ByteArray(") and value.endswith(")")
    if is_bytes:
        value = value[len("@ByteArray(") : -1]
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    result: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            following = value[index + 1]
            if following in {"\\", '"'}:
                result.append(following)
                index += 2
                continue
            if following == "x":
                match = _HEX.match(value, index + 2)
                if match:
                    result.append(chr(int(match.group(0), 16)))
                    index = match.end()
                    continue
        result.append(char)
        index += 1
    text = "".join(result)
    if is_bytes and any(0x80 <= ord(char) <= 0xFF for char in text) and all(ord(char) <= 0xFF for char in text):
        try:
            return text.encode("latin-1").decode("utf-8")
        except UnicodeError:
            return text
    return text


def encode(value: str) -> str:
    """Escape a string the way QSettings' INI writer does."""
    escaped = value.replace("\\", "\\\\")
    if any(char in escaped for char in ';,='):
        return '"' + escaped.replace('"', '\\"') + '"'
    return escaped


def encode_byte_array(value: str) -> str:
    """Render a ``QByteArray`` value (UTF-8 bytes, non-ASCII as ``\\xHH``)."""
    body = []
    for byte in value.encode("utf-8"):
        if byte > 0x7F:
            body.append(f"\\x{byte:02x}")
        elif byte == 0x5C:
            body.append("\\\\")
        else:
            body.append(chr(byte))
    text = "".join(body)
    if any(char in text for char in ";,="):
        text = '"' + text.replace('"', '\\"') + '"'
    return "@ByteArray(" + text + ")"
