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


_HEXDIGITS = "0123456789abcdefABCDEF"
_QCOLOR_TYPE_ID = 67  # QMetaType::QColor


def encode_qcolor(color: str) -> str:
    """Render a ``QColor`` the way QSettings stores it (``@Variant``).

    MO2 saves a separator colour with ``metaFile.setValue("color", QColor)``
    (modinforegular.cpp). QSettings serialises it as a ``QVariant`` data stream:
    int32 type id 67 (QColor), a null flag byte, then the QColor itself: spec
    ``Rgb`` (1) followed by alpha, red, green, blue and a pad as 16-bit values
    (each 8-bit channel scaled by 257).
    """
    text = color.strip().lstrip("#")
    if len(text) != 6 or any(char not in _HEXDIGITS for char in text):
        raise ValueError(f"Colour must look like #RRGGBB: {color}")
    channels = [int(text[index : index + 2], 16) for index in (0, 2, 4)]
    data = bytearray(_QCOLOR_TYPE_ID.to_bytes(4, "big")) + bytes([0, 1])
    for channel in [255] + channels + [0]:
        data += (channel * 257 if channel else 0).to_bytes(2, "big")
    return "@Variant(" + _escape_bytes(bytes(data)) + ")"


def _escape_bytes(data: bytes) -> str:
    backslash = chr(92)
    out: list[str] = []
    after_hex = False
    for byte in data:
        char = chr(byte)
        if byte == 0:
            out.append(backslash + "0")
            after_hex = False
        elif byte < 0x20 or byte >= 0x7F:
            out.append(f"{backslash}x{byte:x}")
            after_hex = True
        elif after_hex and char in _HEXDIGITS:
            out.append(f"{backslash}x{byte:x}")
        elif char == backslash:
            out.append(backslash * 2)
            after_hex = False
        else:
            out.append(char)
            after_hex = False
    return "".join(out)


def decode_qcolor(value: str) -> str | None:
    """Inverse of :func:`encode_qcolor`; ``None`` when the value is no opaque RGB QColor variant."""
    value = value.strip()
    if not (value.startswith("@Variant(") and value.endswith(")")):
        return None
    body = value[len("@Variant(") : -1]
    backslash = chr(92)
    data = bytearray()
    index = 0
    while index < len(body):
        char = body[index]
        if char == backslash and index + 1 < len(body):
            following = body[index + 1]
            if following == "x":
                end = index + 2
                while end < len(body) and end < index + 4 and body[end] in _HEXDIGITS:
                    end += 1
                if end == index + 2:
                    return None
                data.append(int(body[index + 2 : end], 16))
                index = end
                continue
            if following == "0":
                data.append(0)
                index += 2
                continue
            if following == backslash:
                data.append(0x5C)
                index += 2
                continue
        data.append(ord(char) & 0xFF)
        index += 1
    if len(data) != 16 or bytes(data[:4]) != _QCOLOR_TYPE_ID.to_bytes(4, "big") or data[5] != 1:
        return None
    return "#{:02x}{:02x}{:02x}".format(data[8], data[10], data[12])
