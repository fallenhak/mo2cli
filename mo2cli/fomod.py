from __future__ import annotations

import xml.etree.ElementTree as ET
import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .archives import extract_to_temp
from .workspace import Mo2Error


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(node: ET.Element, name: str) -> list[ET.Element]:
    if node is None:
        return []
    return [child for child in list(node) if _local(child.tag).casefold() == name.casefold()]


def _first(node: ET.Element, name: str) -> ET.Element | None:
    return next(iter(_children(node, name)), None)


def _version(value: str) -> tuple[tuple[int, Any], ...]:
    parts: list[tuple[int, Any]] = []
    for token in re.split(r"[.\-_+ ]+", value.strip()):
        if token.isdigit():
            parts.append((0, int(token)))
        elif token:
            parts.append((1, token.casefold()))
    return tuple(parts)


@dataclass
class FomodContext:
    flags: dict[str, str]
    file_states: dict[str, str]
    game_version: str | None = None

    def file_state(self, name: str) -> str:
        return self.file_states.get(Path(name).name.casefold(), self.file_states.get(name.casefold(), "Missing"))


def _condition(node: ET.Element | None, context: FomodContext) -> bool:
    if node is None:
        return True
    tag = _local(node.tag).casefold()
    if tag in {"visible", "dependencies"}:
        conditions = [_condition(child, context) for child in list(node)]
        if not conditions:
            return True
        return all(conditions) if node.attrib.get("operator", "And").casefold() != "or" else any(conditions)
    if tag == "filedependency":
        return context.file_state(node.attrib.get("file", "")) == node.attrib.get("state", "Missing")
    if tag == "flagdependency":
        return context.flags.get(node.attrib.get("flag", ""), "") == node.attrib.get("value", "")
    if tag in {"gamedependency", "fommdependency", "fosedependency"}:
        required = node.attrib.get("version", "")
        return context.game_version is not None and _version(context.game_version) >= _version(required)
    return True


def _files(node: ET.Element | None) -> list[dict[str, object]]:
    if node is None:
        return []
    result = []
    for item in node.iter():
        tag = _local(item.tag).casefold()
        if tag not in {"file", "folder"}:
            continue
        source = item.attrib.get("source", "")
        if not source:
            continue
        raw_priority = item.attrib.get("priority", "0") or "0"
        try:
            priority = int(raw_priority)
        except ValueError:
            raise Mo2Error(f"Invalid FOMOD priority value: {raw_priority}")
        result.append({"source": source.replace("\\", "/"), "destination": item.attrib.get("destination", source).replace("\\", "/"), "priority": priority, "folder": tag == "folder", "installIfUsable": item.attrib.get("installIfUsable", "false").casefold() == "true", "alwaysInstall": item.attrib.get("alwaysInstall", "false").casefold() == "true"})
    return result


def _plugin_type(plugin: ET.Element, context: FomodContext) -> str:
    descriptor = _first(plugin, "typeDescriptor")
    if descriptor is None:
        return "Optional"
    direct = _first(descriptor, "type")
    if direct is not None:
        return direct.attrib.get("name", "Optional")
    dependency = _first(descriptor, "dependencyType")
    if dependency is not None:
        default = _first(dependency, "defaultType")
        patterns = _first(dependency, "patterns")
        for pattern in _children(patterns if patterns is not None else dependency, "pattern"):
            if _condition(_first(pattern, "dependencies"), context):
                type_node = _first(pattern, "type")
                if type_node is not None:
                    return type_node.attrib.get("name", "Optional")
        return default.attrib.get("name", "Optional") if default is not None else "Optional"
    return "Optional"


def _find_config(root: Path) -> Path:
    config = next((path for path in root.rglob("ModuleConfig.xml") if path.is_file()), None)
    if config is None:
        raise Mo2Error("FOMOD ModuleConfig.xml not found in archive.")
    return config


def _parse_config(config: Path) -> ET.Element:
    """Parse a FOMOD config, including the nested group layout used by MO2."""
    try:
        raw = config.read_bytes()
        # A few MO2-compatible installers are UTF-16 files whose XML
        # declaration incorrectly says encoding="utf-8".  ElementTree trusts
        # that declaration when parsing from a path, while MO2 detects the
        # BOM and accepts the file.  Decode BOM-marked XML explicitly so the
        # CLI follows the same compatibility behaviour.
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            encoding = "utf-16" if raw.startswith(b"\xff\xfe") else "utf-16-be"
            return ET.fromstring(raw.decode(encoding))
        return ET.parse(config).getroot()
    except ET.ParseError as error:
        raise Mo2Error(f"Failed to parse FOMOD XML: {error}") from error


def config_hash(root: Path) -> str:
    return hashlib.sha256(_find_config(root).read_bytes()).hexdigest()


def module_name(root: Path) -> str:
    config = _find_config(root)
    xml_root = _parse_config(config)
    node = _first(xml_root, "moduleName")
    return (node.text or "").strip() if node is not None else ""


def reconcile_selections(root: Path, selections: dict[str, list[str]] | None) -> tuple[dict[str, list[str]], list[str]]:
    """Keep saved choices whose group/plugin names still exist in a new FOMOD."""
    if not selections:
        return {}, []
    config = _find_config(root)
    xml_root = _parse_config(config)
    known: dict[str, set[str]] = {}
    for step in _find(xml_root, "installStep"):
        step_name = step.attrib.get("name", "")
        for groups_root in _children(step, "optionalFileGroups"):
            for group in _children(groups_root, "group"):
                group_name = group.attrib.get("name", "")
                plugins_root = _first(group, "plugins")
                names = {plugin.attrib.get("name", "") for plugin in _children(plugins_root, "plugin")}
                known[f"{step_name}/{group_name}"] = names
    result: dict[str, list[str]] = {}
    dropped: list[str] = []
    for key, requested in selections.items():
        names = known.get(key)
        if names is None:
            dropped.extend(f"{key}={name}" for name in requested)
            continue
        kept = [name for name in requested if name in names]
        dropped.extend(f"{key}={name}" for name in requested if name not in names)
        if kept:
            result[key] = kept
    return result, dropped


def _safe_archive_source(base: Path, source: str) -> Path:
    path = base.joinpath(*Path(source.replace("\\", "/")).parts)
    if not path.resolve().is_relative_to(base.resolve()):
        raise Mo2Error(f"FOMOD source path escapes archive: {source}")
    return path


def plan_extracted(root: Path, selections: dict[str, list[str]] | None = None, flags: dict[str, str] | None = None, file_states: dict[str, str] | None = None, game_version: str | None = None) -> dict[str, object]:
    config_path = _find_config(root)
    xml_root = _parse_config(config_path)
    base = config_path.parent.parent
    context = FomodContext({} if flags is None else dict(flags), {} if file_states is None else {key.casefold(): value for key, value in file_states.items()}, game_version)
    selected_names: list[str] = []
    selected_by_group: dict[str, list[str]] = {}
    operations: list[dict[str, object]] = []
    errors: list[str] = []
    module_name = (_first(xml_root, "moduleName").text or "").strip() if _first(xml_root, "moduleName") is not None else ""
    required = _files(_first(xml_root, "requiredInstallFiles"))
    operations.extend(required)
    install_steps = _first(xml_root, "installSteps")
    steps = _children(install_steps, "installStep") if install_steps is not None else []
    order = (install_steps.attrib.get("order", "Ascending") if install_steps is not None else "Ascending").casefold()
    if order == "ascending":
        steps.sort(key=lambda step: step.attrib.get("name", "").casefold())
    elif order == "descending":
        steps.sort(key=lambda step: step.attrib.get("name", "").casefold(), reverse=True)
    step_seen: dict[str, int] = {}
    for step in steps:
        if not _condition(_first(step, "visible"), context):
            continue
        step_name = step.attrib.get("name", "")
        step_seen[step_name] = step_seen.get(step_name, 0) + 1
        step_index = step_seen[step_name]
        # FOMODs sometimes repeat an installStep name (for example several
        # independent patch pages). Keep those pages addressable separately
        # instead of leaking one page's selection into all of them.
        step_key = step_name if step_index == 1 else f"{step_name}#{step_index}"
        groups_root = _first(step, "optionalFileGroups")
        group_seen: dict[str, int] = {}
        for group in _children(groups_root, "group") if groups_root is not None else []:
            group_name = group.attrib.get("name", "")
            group_seen[group_name] = group_seen.get(group_name, 0) + 1
            group_index = group_seen[group_name]
            plugins_root = _first(group, "plugins")
            plugin_nodes = _children(plugins_root, "plugin") if plugins_root is not None else []
            requested = []
            selection_specified = False
            if selections:
                normalized_keys = {str(key).strip(): value for key, value in selections.items()}
                selection_keys = [
                    f"{step_key}/{group_name}#{group_index}",
                    f"{step_key}/{group_name}[{group_index}]",
                    f"{step_key}/{group_name}",
                ]
                if step_index == 1:
                    # Preserve the original unqualified syntax for the first
                    # occurrence of a step name.
                    selection_keys.extend([
                        f"{step_name}/{group_name}#{group_index}",
                        f"{step_name}/{group_name}[{group_index}]",
                        f"{step_name}/{group_name}",
                        group_name,
                    ])
                requested = None
                for selection_key in selection_keys:
                    requested = selections.get(selection_key)
                    if requested is None:
                        requested = normalized_keys.get(selection_key.strip())
                    if requested is not None:
                        selection_specified = True
                        break
                if requested is None:
                    requested = []
            group_type = group.attrib.get("type", "SelectAny")
            plugin_types = {plugin.attrib.get("name", ""): _plugin_type(plugin, context) for plugin in plugin_nodes}
            normalized_plugin_names = {name.strip(): name for name in plugin_types}
            available = [plugin for plugin in plugin_nodes if plugin_types[plugin.attrib.get("name", "")] != "NotUsable"]
            if requested:
                unknown = [name for name in requested if name.strip() not in normalized_plugin_names]
                if unknown:
                    errors.append(f"{step_name}/{group_name}: unknown selection: {', '.join(unknown)}")
                requested = [normalized_plugin_names.get(name.strip(), name) for name in requested]
            chosen_names: set[str]
            if selection_specified:
                chosen_names = set(requested)
            elif group_type == "SelectAll":
                chosen_names = {plugin.attrib.get("name", "") for plugin in available}
            else:
                required_names = [plugin.attrib.get("name", "") for plugin in available if plugin_types[plugin.attrib.get("name", "")] == "Required"]
                recommended_names = [plugin.attrib.get("name", "") for plugin in available if plugin_types[plugin.attrib.get("name", "")] == "Recommended"]
                chosen_names = set(required_names)
                if group_type in {"SelectExactlyOne", "SelectAtMostOne"}:
                    chosen_names = set(required_names[:1] or recommended_names[:1])
                elif not chosen_names:
                    chosen_names = set(recommended_names)
            chosen = [plugin for plugin in plugin_nodes if plugin.attrib.get("name", "") in chosen_names and plugin_types[plugin.attrib.get("name", "")] != "NotUsable"]
            selection_key = f"{step_key}/{group_name}"
            if group_seen[group_name] > 1:
                selection_key += f"#{group_index}"
            selected_by_group[selection_key] = [plugin.attrib.get("name", "") for plugin in chosen]
            for plugin in chosen:
                name = plugin.attrib.get("name", "")
                selected_names.append(name)
                condition_flags = _first(plugin, "conditionFlags")
                for flag in _children(condition_flags if condition_flags is not None else plugin, "flag"):
                    context.flags[flag.attrib.get("name", "")] = (flag.text or "").strip()
            if group_type in {"SelectExactlyOne", "SelectAtLeastOne"} and not chosen:
                errors.append(f"{step_name}/{group_name}: at least one selection required")
            if group_type == "SelectExactlyOne" and len(chosen) > 1:
                errors.append(f"{step_name}/{group_name}: exactly one selection required")
            if group_type == "SelectAtMostOne" and len(chosen) > 1:
                errors.append(f"{step_name}/{group_name}: at most one selection allowed")
            for plugin in chosen:
                operations.extend(_files(_first(plugin, "files")))
    conditional = _first(xml_root, "conditionalFileInstalls")
    patterns = _children(_first(conditional, "patterns") if conditional is not None else None, "pattern")
    for pattern in patterns:
        if _condition(_first(pattern, "dependencies"), context):
            operations.extend(_files(_first(pattern, "files")))
    normalized = []
    indexed_operations = list(enumerate(operations))
    for _, operation in sorted(indexed_operations, key=lambda pair: (int(pair[1].get("priority", 0)), pair[0])):
        source = _safe_archive_source(base, str(operation["source"]))
        if not source.exists():
            errors.append(f"Missing FOMOD source: {operation['source']}")
        normalized.append({**operation, "source_path": str(source)})
    return {"archive_root": str(root), "config": str(config_path.relative_to(root)), "module_name": module_name, "selected": selected_names, "selections": selected_by_group, "operations": normalized, "errors": errors}


def apply_plan(plan: dict[str, object], destination: Path) -> None:
    if plan.get("errors"):
        raise Mo2Error("Failed to apply FOMOD plan: " + "; ".join(str(error) for error in plan["errors"]))
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    for operation in plan["operations"]:
        source = Path(str(operation["source_path"]))
        if source.is_symlink():
            raise Mo2Error(f"FOMOD symlink source not supported: {operation['source']}")
        raw_destination = str(operation.get("destination", "")).replace("\\", "/")
        destination_parts = Path(raw_destination).parts
        if Path(raw_destination).is_absolute() or ".." in destination_parts:
            raise Mo2Error(f"FOMOD destination path is unsafe: {raw_destination}")
        target_root = (destination / Path(*destination_parts)).resolve()
        if not target_root.is_relative_to(destination):
            raise Mo2Error(f"FOMOD destination path escapes mod directory: {raw_destination}")
        if operation.get("folder"):
            if not source.is_dir():
                raise Mo2Error(f"FOMOD folder not found: {operation['source']}")
            target_root.mkdir(parents=True, exist_ok=True)
            for item in source.rglob("*"):
                if item.is_symlink():
                    raise Mo2Error(f"Symlinks inside FOMOD are not supported: {operation['source']}")
                target = target_root / item.relative_to(source)
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, target)
        else:
            if not source.is_file():
                raise Mo2Error(f"FOMOD file not found: {operation['source']}")
            target = target_root
            if raw_destination.endswith("/"):
                target = target_root / source.name
            if not target.resolve().is_relative_to(destination):
                raise Mo2Error(f"FOMOD destination escapes mod directory: {raw_destination}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def public_plan(plan: dict[str, object]) -> dict[str, object]:
    """Remove temporary extraction paths from a plan intended for JSON output."""
    return {
        **plan,
        "operations": [{key: value for key, value in operation.items() if key != "source_path"} for operation in plan["operations"]],
    }


def _find(root: ET.Element, name: str) -> list[ET.Element]:
    return [node for node in root.iter() if node.tag.casefold().endswith(name.casefold())]


def inspect_archive(archive: str | Path) -> dict[str, object]:
    archive_path = Path(archive).expanduser().resolve()
    extracted = extract_to_temp(archive_path)
    try:
        config = next((path for path in extracted.root.rglob("ModuleConfig.xml") if path.is_file()), None)
        if config is None:
            raise Mo2Error("FOMOD ModuleConfig.xml not found in archive.")
        root = _parse_config(config)
        steps = []
        for step in _find(root, "installStep"):
            groups = []
            groups_root = _first(step, "optionalFileGroups")
            for group in _children(groups_root, "group"):
                if not group.tag.casefold().endswith("group"):
                    continue
                plugins = []
                plugins_root = _first(group, "plugins")
                for item in _children(plugins_root, "plugin"):
                    files = []
                    for file_node in _find(item, "file"):
                        files.append({"source": file_node.attrib.get("source"), "destination": file_node.attrib.get("destination", ""), "priority": file_node.attrib.get("priority", "0")})
                    plugins.append({"name": item.attrib.get("name", ""), "type": item.attrib.get("type", "optional"), "description": (next((child.text or "" for child in list(item) if child.tag.casefold().endswith("description")), "")), "files": files})
                groups.append({"name": group.attrib.get("name", ""), "type": group.attrib.get("type", "SelectExactlyOne"), "plugins": plugins})
            steps.append({"name": step.attrib.get("name", ""), "groups": groups})
        return {"archive": str(archive_path), "config": str(config.relative_to(extracted.root)), "steps": steps}
    finally:
        extracted.close()


def plan_archive(archive: str | Path, selections: dict[str, list[str]] | None = None, flags: dict[str, str] | None = None, game_version: str | None = None) -> dict[str, object]:
    archive_path = Path(archive).expanduser().resolve()
    extracted = extract_to_temp(archive_path)
    try:
        plan = public_plan(plan_extracted(extracted.root, selections=selections, flags=flags, game_version=game_version))
        plan["archive"] = str(archive_path)
        return plan
    finally:
        extracted.close()
