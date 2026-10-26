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
    (modinforegular.cpp). QSettings serialises variants with a
    ``QDataStream`` pinned to ``Qt_4_0`` (qsettings.cpp ``variantToString``),
    which has no null flag: int32 type id 67 (QColor), then spec ``Rgb`` (1)
    and alpha, red, green, blue and a pad as 16-bit values (each 8-bit channel
    scaled by 257).
    """
    text = color.strip().lstrip("#")
    if len(text) != 6 or any(char not in _HEXDIGITS for char in text):
        raise ValueError(f"Colour must look like #RRGGBB: {color}")
    channels = [int(text[index : index + 2], 16) for index in (0, 2, 4)]
    data = bytearray(_QCOLOR_TYPE_ID.to_bytes(4, "big")) + bytes([1])
    for channel in [255] + channels + [0]:
        data += (channel * 257).to_bytes(2, "big")
    escaped, needs_quotes = _escape_bytes(bytes(data))
    value = "@Variant(" + escaped + ")"
    return f'"{value}"' if needs_quotes else value


_NAMED_ESCAPES = {7: "a", 8: "b", 12: "f", 10: "n", 13: "r", 9: "t", 11: "v"}


def _escape_bytes(data: bytes) -> tuple[str, bool]:
    """Qt's ``iniEscapedString`` for latin-1 bytes; also reports whether the value needs quotes."""
    backslash = chr(92)
    out: list[str] = []
    escape_next_if_digit = False
    needs_quotes = False
    for byte in data:
        char = chr(byte)
        if escape_next_if_digit and char in _HEXDIGITS:
            out.append(f"{backslash}x{byte:x}")
            continue
        escape_next_if_digit = False
        if byte == 0:
            out.append(backslash + "0")
            escape_next_if_digit = True
        elif byte in _NAMED_ESCAPES:
            out.append(backslash + _NAMED_ESCAPES[byte])
        elif char in ('"', backslash):
            out.append(backslash + char)
        elif char in ";,=":
            needs_quotes = True
            out.append(char)
        elif byte < 0x20 or byte >= 0x7F:
            out.append(f"{backslash}x{byte:x}")
            escape_next_if_digit = True
        else:
            out.append(char)
    return "".join(out), needs_quotes


def decode_qcolor(value: str) -> str | None:
    """Inverse of :func:`encode_qcolor`; ``None`` when the value is no opaque RGB QColor variant."""
    value = value.strip()
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    if not (value.startswith("@Variant(") and value.endswith(")")):
        return None
    body = value[len("@Variant(") : -1]
    backslash = chr(92)
    named = {letter: byte for byte, letter in _NAMED_ESCAPES.items()}
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
            if following in named:
                data.append(named[following])
                index += 2
                continue
            data.append(ord(following) & 0xFF)
            index += 2
            continue
        data.append(ord(char) & 0xFF)
        index += 1
    if len(data) != 15 or bytes(data[:4]) != _QCOLOR_TYPE_ID.to_bytes(4, "big") or data[4] != 1:
        return None
    return "#{:02x}{:02x}{:02x}".format(data[7], data[9], data[11])
