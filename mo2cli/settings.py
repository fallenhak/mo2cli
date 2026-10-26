"""Read and edit the instance's ``ModOrganizer.ini`` (not the profile INIs).

Edits are line based so every byte outside the touched line stays as it was
(line endings, comments, unknown sections). Values go through the QSettings
codec in :mod:`qsettings`; ``raw=True`` bypasses it.
"""

from __future__ import annotations

from . import qsettings
from .formats import read_text_exact, write_text
from .journal import record
from .workspace import Instance, Mo2Error


def _lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def _header(line: str) -> str | None:
    stripped = line.strip()
    if len(stripped) >= 2 and stripped.startswith("[") and stripped.endswith("]"):
        return stripped[1:-1]
    return None


def _split_key(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped[0] in ";#[" or "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    return key.strip(), value.strip()


def _section_range(lines: list[str], section: str) -> tuple[int, int] | None:
    """Half open line range of the body of ``section`` (``-`` is the part before the first header)."""
    if section == "-":
        for index, line in enumerate(lines):
            if _header(line) is not None:
                return 0, index
        return 0, len(lines)
    start = None
    for index, line in enumerate(lines):
        name = _header(line)
        if name is None:
            continue
        if start is not None:
            return start, index
        if name.casefold() == section.casefold():
            start = index + 1
    return (start, len(lines)) if start is not None else None


def _find(lines: list[str], bounds: tuple[int, int], key: str) -> int | None:
    exact = fold = None
    for index in range(*bounds):
        pair = _split_key(lines[index])
        if pair is None:
            continue
        if pair[0] == key:
            exact = index
        elif fold is None and pair[0].casefold() == key.casefold():
            fold = index
    return exact if exact is not None else fold


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _read(instance: Instance) -> str:
    if not instance.ini.is_file():
        raise Mo2Error(f"ModOrganizer.ini not found: {instance.ini}")
    try:
        return read_text_exact(instance.ini)
    except UnicodeDecodeError as error:
        raise Mo2Error(f"ModOrganizer.ini is not valid UTF-8: {error}") from error


def list_settings(instance: Instance, section: str | None = None, raw: bool = False) -> list[dict[str, object]]:
    lines = _lines(_read(instance))
    if section is None:
        names = []
        for line in lines:
            name = _header(line)
            if name is not None and name not in names:
                names.append(name)
        return [{"section": name} for name in names]
    bounds = _section_range(lines, section)
    if bounds is None:
        raise Mo2Error(f"Section not found: {section}")
    items = []
    for index in range(*bounds):
        pair = _split_key(lines[index])
        if pair:
            items.append({"section": section, "key": pair[0], "value": pair[1] if raw else qsettings.decode(pair[1])})
    return items


def get_setting(instance: Instance, section: str, key: str, raw: bool = False) -> dict[str, object]:
    lines = _lines(_read(instance))
    bounds = _section_range(lines, section)
    index = _find(lines, bounds, key) if bounds else None
    if index is None:
        raise Mo2Error(f"Setting not found: [{section}] {key}")
    stored = _split_key(lines[index])[1]  # type: ignore[index]
    return {"section": section, "key": key, "value": stored if raw else qsettings.decode(stored), "raw": stored}


def set_setting(instance: Instance, section: str, key: str, value: str, raw: bool = False, as_bytearray: bool = False, dry_run: bool = False) -> dict[str, object]:
    if raw and as_bytearray:
        raise Mo2Error("--raw and --bytearray cannot be combined.")
    if not key or "=" in key or "\n" in key or "\n" in value or "\r" in value:
        raise Mo2Error("Key or value contains characters that cannot be written on one INI line.")
    text = _read(instance)
    stored = value if raw else qsettings.encode_byte_array(value) if as_bytearray else qsettings.encode(value)
    lines = _lines(text)
    newline = _newline(text)
    bounds = _section_range(lines, section)
    previous = None
    index = _find(lines, bounds, key) if bounds else None
    if index is not None:
        old_line = lines[index]
        previous = _split_key(old_line)[1]  # type: ignore[index]
        ending = old_line[len(old_line.rstrip("\r\n")):]
        indent = old_line[: len(old_line) - len(old_line.lstrip())]
        original_key = _split_key(old_line)[0]  # type: ignore[index]
        lines[index] = f"{indent}{original_key}={stored}{ending}"
        if not ending and index != len(lines) - 1:
            lines[index] += newline
    elif bounds is not None:
        insert_at = bounds[1]
        while insert_at > bounds[0] and not lines[insert_at - 1].strip():
            insert_at -= 1
        if insert_at > 0 and not lines[insert_at - 1].endswith(("\n", "\r")):
            lines[insert_at - 1] += newline
        lines.insert(insert_at, f"{key}={stored}{newline}")
    elif section == "-":
        lines.insert(0, f"{key}={stored}{newline}")
    else:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += newline
        if lines and lines[-1].strip():
            lines.append(newline)
        lines.extend([f"[{section}]{newline}", f"{key}={stored}{newline}"])
    updated = "".join(lines)
    result: dict[str, object] = {"section": section, "key": key, "value": stored, "previous": previous, "changed": updated != text, "dry_run": dry_run}
    if dry_run or updated == text:
        return result
    write_text(instance.ini, updated)
    entry = record(
        instance,
        "settings_set",
        section=section,
        key=key,
        state_files=[{"path": str(instance.ini), "exists": True, "content": text}],
    )
    result["journal_id"] = entry["id"]
    return result
