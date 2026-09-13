from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from .workspace import Instance, Mo2Error


def _path_inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _journal_path(instance: Instance) -> Path:
    return instance.base / ".mo2cli-journal.jsonl"


def _file_condition(path: Path) -> dict[str, object]:
    condition: dict[str, object] = {"path": str(path), "exists": path.is_file()}
    if path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        condition["sha256"] = digest.hexdigest()
    return condition


def _read(instance: Instance) -> list[dict[str, Any]]:
    path = _journal_path(instance)
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            entries.append(value)
    return entries


def record(instance: Instance, operation: str, reversible: bool = True, **details: Any) -> dict[str, Any]:
    entry = {
        "id": uuid.uuid4().hex,
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "operation": operation,
        "reversible": reversible,
        **details,
    }
    tracked_paths = {
        str(item["path"]): Path(str(item["path"]))
        for group in (details.get("profiles", []), details.get("state_files", []))
        for item in group
        if isinstance(item, dict) and item.get("path")
    }
    if reversible:
        entry["postconditions"] = [_file_condition(path) for path in tracked_paths.values()]
    path = _journal_path(instance)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def history(instance: Instance, limit: int = 20) -> list[dict[str, Any]]:
    return _read(instance)[-max(1, limit):]


def _restore_profiles(instance: Instance, details: dict[str, Any]) -> None:
    for item in details.get("profiles", []):
        path = Path(str(item["path"]))
        if not _path_inside(path, instance.profiles_dir):
            raise Mo2Error("Journal profile target is outside profiles directory; operation aborted.")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(item.get("content", "")), encoding="utf-8", newline="")


def _restore_state_files(instance: Instance, details: dict[str, Any]) -> None:
    allowed_roots = (instance.profiles_dir, instance.downloads_dir)
    for item in details.get("state_files", []):
        path = Path(str(item["path"]))
        if path.resolve() != instance.ini.resolve() and not any(_path_inside(path, root) for root in allowed_roots):
            raise Mo2Error("Journal state target is outside safe directories; operation aborted.")
        if item.get("exists"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(item.get("content", "")), encoding="utf-8", newline="")
        elif path.exists():
            path.unlink()


def _validate_postconditions(instance: Instance, details: dict[str, Any]) -> None:
    conditions = details.get("postconditions")
    if not isinstance(conditions, list):
        raise Mo2Error("Journal entry predates undo freshness checks; refusing to overwrite current state.")
    allowed_roots = (instance.profiles_dir, instance.downloads_dir)
    for expected in conditions:
        if not isinstance(expected, dict) or not expected.get("path"):
            raise Mo2Error("Journal entry contains an invalid undo precondition.")
        path = Path(str(expected["path"]))
        if path.resolve() != instance.ini.resolve() and not any(_path_inside(path, root) for root in allowed_roots):
            raise Mo2Error("Journal precondition target is outside safe directories; operation aborted.")
        current = _file_condition(path)
        if current.get("exists") != expected.get("exists") or current.get("sha256") != expected.get("sha256"):
            raise Mo2Error(f"State changed since the recorded operation; undo refused: {path}")


def undo_last(instance: Instance) -> dict[str, Any]:
    entries = _read(instance)
    undone = {str(entry.get("target")) for entry in entries if entry.get("operation") == "undo"}
    target = next((entry for entry in reversed(entries) if entry.get("operation") in {"install", "remove", "rename", "separator_create", "separator_remove", "separator_group", "profile_delete"} and entry.get("reversible") and str(entry.get("id")) not in undone), None)
    if target is None:
        raise Mo2Error("No reversible operation found.")

    _validate_postconditions(instance, target)
    operation = target["operation"]
    if operation == "install":
        destination = Path(str(target["destination"]))
        if not _path_inside(destination, instance.mods_dir):
            raise Mo2Error("Undo target is outside mods directory; operation aborted.")
        if destination.exists():
            recovery = instance.base / ".mo2cli-trash" / f"undo-{destination.name}-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
            recovery.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(destination), str(recovery))
        replaced = target.get("replaced")
        if replaced:
            old = Path(str(replaced))
            if old.exists() and _path_inside(old, instance.base / ".mo2cli-trash"):
                shutil.move(str(old), str(destination))
        _restore_profiles(instance, target)
        _restore_state_files(instance, target)
    elif operation == "remove":
        destination = Path(str(target["destination"]))
        trash = Path(str(target.get("trash") or ""))
        if not _path_inside(destination, instance.mods_dir) or not _path_inside(trash, instance.base / ".mo2cli-trash"):
            raise Mo2Error("Undo target is outside safe directories; operation aborted.")
        if destination.exists():
            raise Mo2Error(f"Mod target already exists, not overwriting: {destination}")
        if not trash.is_dir():
            raise Mo2Error(f"Trash copy not found: {trash}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(trash), str(destination))
        _restore_profiles(instance, target)
    elif operation == "rename":
        old = Path(str(target["old_path"]))
        new = Path(str(target["new_path"]))
        if not _path_inside(old, instance.mods_dir) or not _path_inside(new, instance.mods_dir):
            raise Mo2Error("Undo target is outside mods directory; operation aborted.")
        if old.exists() or not new.is_dir():
            raise Mo2Error("Expected mod paths for rename undo have changed.")
        new.rename(old)
        _restore_profiles(instance, target)
    elif operation == "separator_create":
        path = Path(str(target["path"]))
        if not _path_inside(path, instance.mods_dir):
            raise Mo2Error("Separator undo target is outside mods directory; operation aborted.")
        if path.exists() and target.get("created_path", True):
            unexpected = [item for item in path.iterdir() if item.name.casefold() != "meta.ini"]
            if unexpected:
                raise Mo2Error(f"Separator contains unexpected files, not removed: {path}")
            shutil.rmtree(path)
        _restore_profiles(instance, target)
    elif operation == "separator_remove":
        path = Path(str(target["path"]))
        trash = Path(str(target.get("trash") or ""))
        if not _path_inside(path, instance.mods_dir) or not _path_inside(trash, instance.base / ".mo2cli-trash"):
            raise Mo2Error("Separator undo target is outside safe directories; operation aborted.")
        if path.exists():
            raise Mo2Error(f"Separator target already exists, not overwriting: {path}")
        if not trash.is_dir():
            raise Mo2Error(f"Separator trash copy not found: {trash}")
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(trash), str(path))
        _restore_profiles(instance, target)
    elif operation == "separator_group":
        _restore_profiles(instance, target)
    elif operation == "profile_delete":
        destination = Path(str(target["destination"]))
        trash = Path(str(target.get("trash") or ""))
        if not _path_inside(destination, instance.profiles_dir) or not _path_inside(trash, instance.base / ".mo2cli-trash"):
            raise Mo2Error("Profile undo target is outside safe directories; operation aborted.")
        if destination.exists():
            raise Mo2Error(f"Profile target already exists, not overwriting: {destination}")
        if not trash.is_dir():
            raise Mo2Error(f"Profile trash copy not found: {trash}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(trash), str(destination))
        _restore_state_files(instance, target)
        previous_selected = target.get("previous_selected")
        if isinstance(previous_selected, str):
            instance.selected_profile = previous_selected

    result = {"undone": target["operation"], "id": target["id"]}
    record(instance, "undo", reversible=False, target=target["id"], result=result)
    return result
