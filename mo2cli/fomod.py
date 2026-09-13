from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import Counter
import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .archives import extract_to_temp
from .workspace import Instance, Mo2Error


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
    fomm_version: str | None = "0.13.21"
    script_extender_version: str | None = None

    def file_state(self, name: str) -> str:
        return self.file_states.get(Path(name).name.casefold(), self.file_states.get(name.casefold(), "Missing"))


def _condition(node: ET.Element | None, context: FomodContext) -> bool:
    if node is None:
        return True
    tag = _local(node.tag).casefold()
    if tag in {"visible", "dependencies", "moduledependencies"}:
        conditions = [_condition(child, context) for child in list(node)]
        if not conditions:
            return True
        return all(conditions) if node.attrib.get("operator", "And").casefold() != "or" else any(conditions)
    if tag == "filedependency":
        return context.file_state(node.attrib.get("file", "")) == node.attrib.get("state", "Missing")
    if tag == "flagdependency":
        return context.flags.get(node.attrib.get("flag", ""), "") == node.attrib.get("value", "")
    if tag == "gamedependency":
        required = node.attrib.get("version", "")
        return context.game_version is not None and _version(context.game_version) >= _version(required)
    if tag == "fommdependency":
        required = node.attrib.get("version", "")
        return context.fomm_version is not None and _version(context.fomm_version) >= _version(required)
    if tag == "fosedependency":
        required = node.attrib.get("version", "")
        return context.script_extender_version is not None and _version(context.script_extender_version) >= _version(required)
    raise Mo2Error(f"Unsupported FOMOD dependency element: {_local(node.tag)}")


def _condition_details(node: ET.Element | None, context: FomodContext) -> dict[str, object] | None:
    if node is None:
        return None
    tag = _local(node.tag)
    detail: dict[str, object] = {"kind": tag, "matched": _condition(node, context)}
    if node.attrib:
        detail["attributes"] = dict(node.attrib)
    children = [_condition_details(child, context) for child in list(node)]
    if children:
        detail["children"] = [child for child in children if child is not None]
    return detail


def _ordered_children(node: ET.Element | None, name: str) -> list[ET.Element]:
    items = _children(node, name) if node is not None else []
    order = (node.attrib.get("order", "Ascending") if node is not None else "Ascending").casefold()
    if order == "ascending":
        items.sort(key=lambda item: item.attrib.get("name", "").casefold())
    elif order == "descending":
        items.sort(key=lambda item: item.attrib.get("name", "").casefold(), reverse=True)
    elif order != "explicit":
        raise Mo2Error(f"Unsupported FOMOD order: {node.attrib.get('order') if node is not None else order}")
    return items


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


def _plugin_type_details(plugin: ET.Element, context: FomodContext) -> dict[str, object]:
    descriptor = _first(plugin, "typeDescriptor")
    if descriptor is None:
        return {"effective": "Optional", "default": "Optional", "patterns": []}
    direct = _first(descriptor, "type")
    if direct is not None:
        name = direct.attrib.get("name", "Optional")
        return {"effective": name, "default": name, "patterns": []}
    dependency = _first(descriptor, "dependencyType")
    if dependency is None:
        return {"effective": "Optional", "default": "Optional", "patterns": []}
    default_node = _first(dependency, "defaultType")
    default = default_node.attrib.get("name", "Optional") if default_node is not None else "Optional"
    patterns_root = _first(dependency, "patterns")
    patterns = []
    effective = default
    matched_type = False
    for pattern in _children(patterns_root if patterns_root is not None else dependency, "pattern"):
        dependencies = _first(pattern, "dependencies")
        matched = _condition(dependencies, context)
        type_node = _first(pattern, "type")
        pattern_type = type_node.attrib.get("name", "Optional") if type_node is not None else "Optional"
        patterns.append({"type": pattern_type, "matched": matched, "dependencies": _condition_details(dependencies, context)})
        if matched and not matched_type:
            effective = pattern_type
            matched_type = True
    return {"effective": effective, "default": default, "patterns": patterns}


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


def dependency_files(root: Path) -> set[str]:
    xml_root = _parse_config(_find_config(root))
    return {
        node.attrib.get("file", "")
        for node in _find(xml_root, "fileDependency")
        if node.attrib.get("file", "")
    }


def load_fomod_plus_record(path: str | Path, identifier: str) -> dict[str, object]:
    database = Path(path).expanduser().resolve()
    try:
        value = json.loads(database.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise Mo2Error(f"Failed to read FOMOD Plus database: {database}: {error}") from error
    if not isinstance(value, list):
        raise Mo2Error(f"Invalid FOMOD Plus database: {database}")
    wanted = identifier.casefold().strip()
    matches = [
        item for item in value
        if isinstance(item, dict) and (
            str(item.get("displayName", "")).casefold() == wanted
            or str(item.get("modId", "")).casefold() == wanted
        )
    ]
    if not matches:
        raise Mo2Error(f"FOMOD Plus record not found: {identifier}")
    if len(matches) > 1:
        raise Mo2Error(f"FOMOD Plus record is ambiguous: {identifier}")
    return matches[0]


def selections_from_fomod_plus(root: Path, record: dict[str, object]) -> dict[str, list[str]]:
    """Map FOMOD Plus choices to stable occurrence-qualified mo2cli keys."""
    raw_options = record.get("options", [])
    options = [item for item in raw_options if isinstance(item, dict)] if isinstance(raw_options, list) else []
    selected_records = [
        (str(item.get("step", "")), str(item.get("group", "")), str(item.get("name", "")))
        for item in options
        if str(item.get("selectionState", "")).casefold() in {"selected", "required"}
    ]
    consumed: set[int] = set()
    xml_root = _parse_config(_find_config(root))
    install_steps = _first(xml_root, "installSteps")
    step_seen: dict[str, int] = {}
    result: dict[str, list[str]] = {}
    for step in _ordered_children(install_steps, "installStep"):
        step_name = step.attrib.get("name", "")
        step_seen[step_name] = step_seen.get(step_name, 0) + 1
        step_index = step_seen[step_name]
        step_key = step_name if step_index == 1 else f"{step_name}#{step_index}"
        groups_root = _first(step, "optionalFileGroups")
        group_seen: dict[str, int] = {}
        for group in _ordered_children(groups_root, "group"):
            group_name = group.attrib.get("name", "")
            group_seen[group_name] = group_seen.get(group_name, 0) + 1
            group_index = group_seen[group_name]
            group_key = f"{step_key}/{group_name}" + (f"#{group_index}" if group_index > 1 else "")
            plugins_root = _first(group, "plugins")
            plugin_names = {plugin.attrib.get("name", "") for plugin in _ordered_children(plugins_root, "plugin")}
            recorded_for_group = [
                item for item in options
                if str(item.get("step", "")) == step_name
                and str(item.get("group", "")) == group_name
                and str(item.get("name", "")) in plugin_names
            ]
            if not recorded_for_group:
                continue
            chosen: list[str] = []
            for index, (record_step, record_group, record_name) in enumerate(selected_records):
                if index in consumed:
                    continue
                if record_step == step_name and record_group == group_name and record_name in plugin_names:
                    chosen.append(record_name)
                    consumed.add(index)
            result[group_key] = chosen
    return result


def compare_fomod_plus(plan: dict[str, object], record: dict[str, object]) -> dict[str, object]:
    raw_options = record.get("options", [])
    selected_records = [
        item for item in raw_options if isinstance(item, dict)
        and str(item.get("selectionState", "")).casefold() in {"selected", "required"}
    ] if isinstance(raw_options, list) else []
    visible_options: Counter[tuple[str, str, str]] = Counter()
    for step in plan.get("steps", []):
        if not isinstance(step, dict):
            continue
        for group in step.get("groups", []):
            if not isinstance(group, dict):
                continue
            for option in group.get("options", []):
                if isinstance(option, dict):
                    visible_options[(str(step.get("name", "")), str(group.get("name", "")), str(option.get("name", "")))] += 1
    recorded_identities: list[tuple[str, str, str]] = []
    ignored_hidden: list[str] = []
    for item in selected_records:
        identity = (str(item.get("step", "")), str(item.get("group", "")), str(item.get("name", "")))
        if visible_options[identity] > 0:
            recorded_identities.append(identity)
            visible_options[identity] -= 1
        else:
            ignored_hidden.append(identity[2])
    planned_identities: list[tuple[str, str, str]] = []
    for step in plan.get("steps", []):
        if not isinstance(step, dict):
            continue
        for group in step.get("groups", []):
            if not isinstance(group, dict):
                continue
            for option in group.get("options", []):
                if isinstance(option, dict) and option.get("selected"):
                    planned_identities.append((
                        str(step.get("name", "")),
                        str(group.get("name", "")),
                        str(option.get("name", "")),
                    ))
    recorded_counts = Counter(recorded_identities)
    planned_counts = Counter(planned_identities)
    missing_identities = list((recorded_counts - planned_counts).elements())
    unexpected_identities = list((planned_counts - recorded_counts).elements())

    def label(identity: tuple[str, str, str]) -> str:
        return "/".join(identity)

    return {
        "display_name": record.get("displayName"),
        "mod_id": record.get("modId"),
        "recorded": [identity[2] for identity in recorded_identities],
        "planned": [identity[2] for identity in planned_identities],
        "recorded_identities": [label(identity) for identity in recorded_identities],
        "planned_identities": [label(identity) for identity in planned_identities],
        "ignored_hidden": ignored_hidden,
        "missing": [label(identity) for identity in missing_identities],
        "unexpected": [label(identity) for identity in unexpected_identities],
        "matches": not missing_identities and not unexpected_identities and not plan.get("errors"),
    }


def reconcile_selections(root: Path, selections: dict[str, list[str]] | None) -> tuple[dict[str, list[str]], list[str]]:
    """Keep saved choices whose group/plugin names still exist in a new FOMOD."""
    if not selections:
        return {}, []
    config = _find_config(root)
    xml_root = _parse_config(config)
    known: dict[str, set[str]] = {}
    install_steps = _first(xml_root, "installSteps")
    step_seen: dict[str, int] = {}
    for step in _ordered_children(install_steps, "installStep"):
        step_name = step.attrib.get("name", "")
        step_seen[step_name] = step_seen.get(step_name, 0) + 1
        step_index = step_seen[step_name]
        step_key = step_name if step_index == 1 else f"{step_name}#{step_index}"
        groups_root = _first(step, "optionalFileGroups")
        group_seen: dict[str, int] = {}
        for group in _ordered_children(groups_root, "group"):
            group_name = group.attrib.get("name", "")
            group_seen[group_name] = group_seen.get(group_name, 0) + 1
            group_index = group_seen[group_name]
            group_key = f"{step_key}/{group_name}"
            if group_index > 1:
                group_key += f"#{group_index}"
            plugins_root = _first(group, "plugins")
            known[group_key] = {plugin.attrib.get("name", "") for plugin in _ordered_children(plugins_root, "plugin")}
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


def plan_extracted(
    root: Path,
    selections: dict[str, list[str]] | None = None,
    flags: dict[str, str] | None = None,
    file_states: dict[str, str] | None = None,
    game_version: str | None = None,
    fomm_version: str | None = "0.13.21",
    script_extender_version: str | None = None,
) -> dict[str, object]:
    config_path = _find_config(root)
    xml_root = _parse_config(config_path)
    base = config_path.parent.parent
    context = FomodContext(
        {} if flags is None else dict(flags),
        {} if file_states is None else {key.casefold(): value for key, value in file_states.items()},
        game_version,
        fomm_version,
        script_extender_version,
    )
    selected_names: list[str] = []
    selected_by_group: dict[str, list[str]] = {}
    operations: list[dict[str, object]] = []
    errors: list[str] = []
    page_details: list[dict[str, object]] = []
    module_name = (_first(xml_root, "moduleName").text or "").strip() if _first(xml_root, "moduleName") is not None else ""
    module_dependencies = _first(xml_root, "moduleDependencies")
    module_dependency_details = _condition_details(module_dependencies, context)
    if module_dependencies is not None and not _condition(module_dependencies, context):
        errors.append("Module dependencies are not satisfied")
    required = _files(_first(xml_root, "requiredInstallFiles"))
    operations.extend(required)
    install_steps = _first(xml_root, "installSteps")
    steps = _ordered_children(install_steps, "installStep") if install_steps is not None else []
    step_seen: dict[str, int] = {}
    for step in steps:
        visibility_node = _first(step, "visible")
        if not _condition(visibility_node, context):
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
        step_detail: dict[str, object] = {
            "key": step_key,
            "name": step_name,
            "visible": True,
            "visibility": _condition_details(visibility_node, context),
            "groups": [],
        }
        for group in _ordered_children(groups_root, "group") if groups_root is not None else []:
            group_name = group.attrib.get("name", "")
            group_seen[group_name] = group_seen.get(group_name, 0) + 1
            group_index = group_seen[group_name]
            plugins_root = _first(group, "plugins")
            plugin_nodes = _ordered_children(plugins_root, "plugin") if plugins_root is not None else []
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
            if group_type not in {"SelectExactlyOne", "SelectAtMostOne", "SelectAtLeastOne", "SelectAll", "SelectAny"}:
                errors.append(f"{step_name}/{group_name}: unsupported group type: {group_type}")
            plugin_type_details = {plugin.attrib.get("name", ""): _plugin_type_details(plugin, context) for plugin in plugin_nodes}
            plugin_types = {name: str(details["effective"]) for name, details in plugin_type_details.items()}
            normalized_plugin_names = {name.strip(): name for name in plugin_types}
            available = [plugin for plugin in plugin_nodes if plugin_types[plugin.attrib.get("name", "")] != "NotUsable"]
            if requested:
                unknown = [name for name in requested if name.strip() not in normalized_plugin_names]
                if unknown:
                    errors.append(f"{step_name}/{group_name}: unknown selection: {', '.join(unknown)}")
                requested = [normalized_plugin_names.get(name.strip(), name) for name in requested]
                unavailable = [name for name in requested if plugin_types.get(name) == "NotUsable"]
                if unavailable:
                    errors.append(f"{step_name}/{group_name}: unavailable selection: {', '.join(unavailable)}")
            required_names = [plugin.attrib.get("name", "") for plugin in available if plugin_types[plugin.attrib.get("name", "")] == "Required"]
            chosen_names: set[str]
            if group_type == "SelectAll":
                chosen_names = {plugin.attrib.get("name", "") for plugin in available}
            elif selection_specified:
                chosen_names = set(requested) | set(required_names)
            else:
                recommended_names = [plugin.attrib.get("name", "") for plugin in available if plugin_types[plugin.attrib.get("name", "")] == "Recommended"]
                chosen_names = set(required_names)
                if group_type in {"SelectExactlyOne", "SelectAtMostOne"}:
                    # Never discard a Required option merely to make a malformed
                    # group satisfy its cardinality. Keep every requirement and
                    # let validation fail closed when the XML is contradictory.
                    chosen_names = set(required_names or recommended_names[:1])
                elif not chosen_names:
                    chosen_names = set(recommended_names)
                if group_type in {"SelectExactlyOne", "SelectAtLeastOne"} and not chosen_names and len(available) == 1:
                    chosen_names = {available[0].attrib.get("name", "")}
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
            option_details: list[dict[str, object]] = []
            for plugin in plugin_nodes:
                name = plugin.attrib.get("name", "")
                plugin_files = _files(_first(plugin, "files"))
                is_chosen = plugin in chosen
                if is_chosen:
                    operations.extend(plugin_files)
                else:
                    operations.extend(
                        operation for operation in plugin_files
                        if operation["alwaysInstall"] or (operation["installIfUsable"] and plugin_types[name] != "NotUsable")
                    )
                description = _first(plugin, "description")
                image = _first(plugin, "image")
                condition_flags = _first(plugin, "conditionFlags")
                option_details.append({
                    "name": name,
                    "type": plugin_types[name],
                    "default_type": plugin_type_details[name]["default"],
                    "available": plugin_types[name] != "NotUsable",
                    "selected": is_chosen,
                    "description": "".join(description.itertext()).strip() if description is not None else "",
                    "image": image.attrib.get("path") if image is not None else None,
                    "flags": {flag.attrib.get("name", ""): (flag.text or "").strip() for flag in _children(condition_flags, "flag")},
                    "type_patterns": plugin_type_details[name]["patterns"],
                    "files": plugin_files,
                })
            step_detail["groups"].append({
                "key": selection_key,
                "name": group_name,
                "type": group_type,
                "options": option_details,
                "plugins": option_details,
            })
        page_details.append(step_detail)
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
    return {
        "archive_root": str(root),
        "config": str(config_path.relative_to(root)),
        "module_name": module_name,
        "selected": selected_names,
        "selections": selected_by_group,
        "operations": normalized,
        "errors": errors,
        "steps": page_details,
        "context": {
            "game_version": context.game_version,
            "fomm_version": context.fomm_version,
            "script_extender_version": context.script_extender_version,
            "file_states": dict(sorted(context.file_states.items())),
            "flags": dict(context.flags),
            # Module dependencies are evaluated before any installer choice can
            # mutate flags. Preserve the exact decision-time evidence here.
            "module_dependencies": module_dependency_details,
        },
    }


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


def inspect_archive(
    archive: str | Path,
    instance: Instance | None = None,
    profile: str | None = None,
    selections: dict[str, list[str]] | None = None,
    flags: dict[str, str] | None = None,
    game_version: str | None = None,
    fomod_plus_record: dict[str, object] | None = None,
) -> dict[str, object]:
    archive_path = Path(archive).expanduser().resolve()
    extracted = extract_to_temp(archive_path)
    try:
        effective_game_version = game_version or (instance.game_version() if instance is not None else None)
        file_states = instance.fomod_file_states(profile, dependency_files(extracted.root)) if instance is not None else None
        script_extender_version = instance.script_extender_version() if instance is not None else None
        effective_selections = selections
        if fomod_plus_record is not None and not effective_selections:
            effective_selections = selections_from_fomod_plus(extracted.root, fomod_plus_record)
        plan = public_plan(plan_extracted(
            extracted.root,
            selections=effective_selections,
            flags=flags,
            file_states=file_states,
            game_version=effective_game_version,
            script_extender_version=script_extender_version,
        ))
        result = {
            "archive": str(archive_path),
            "contextual": instance is not None,
            **{key: value for key, value in plan.items() if key != "archive_root"},
        }
        if fomod_plus_record is not None:
            result["fomod_plus"] = compare_fomod_plus(plan, fomod_plus_record)
        return result
    finally:
        extracted.close()


def plan_archive(
    archive: str | Path,
    selections: dict[str, list[str]] | None = None,
    flags: dict[str, str] | None = None,
    game_version: str | None = None,
    file_states: dict[str, str] | None = None,
    fomm_version: str | None = "0.13.21",
    script_extender_version: str | None = None,
) -> dict[str, object]:
    archive_path = Path(archive).expanduser().resolve()
    extracted = extract_to_temp(archive_path)
    try:
        plan = public_plan(plan_extracted(
            extracted.root,
            selections=selections,
            flags=flags,
            file_states=file_states,
            game_version=game_version,
            fomm_version=fomm_version,
            script_extender_version=script_extender_version,
        ))
        plan["archive"] = str(archive_path)
        return plan
    finally:
        extracted.close()
