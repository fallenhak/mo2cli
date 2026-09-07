from __future__ import annotations

import configparser
import hashlib
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import py7zr

from mo2cli.archives import extract_to_temp, list_archive, sha256
from mo2cli.fomod import (
    FomodContext,
    _children,
    _files,
    _find_config,
    _first,
    _parse_config,
    _plugin_type,
    config_hash,
    plan_extracted,
)
from mo2cli.fomod_decisions import _read, decision_path, save
from mo2cli.formats import ModList
from mo2cli.installer import _decision_payload
from mo2cli.plugins import _active_plugin_files
from mo2cli.workspace import Instance


ARCHIVES = [
    "Ancient Nord Armors and Weapons Retexture SE 91136 2.0.2 2026-07-14T13-05Z VMSnJrLDh.7z",
    "BnP female skin 4k (CBBE Player and Replacer)-65274-2-0-1688931352.7z",
    "BnP male skin 2k (SOS full Player Replacer)-65402-2-1-1704039187.7z",
    "Caliente's Beautiful Bodies Enhancer CBBE - v2.0.3-198-2-0-3-1712683181.7z",
    "CBBE 3BA (3BBB)-30174-2-48-1740765899.zip",
    "CBPC - Fomod installer - MAIN FILE-21224-1-6-4-1708114426.rar",
    "Dragon Armors and Weapons Retexture SE-83222-2-2-2-1781038333.7z",
    "ELFX Shadows 63790 1.6.2 2026-07-05T23-53Z L5WQbqz7n.7z",
    "Elven Armors and Weapons Retexture SE-87283-2-0-1-1781095370.7z",
    "Engine Fixes - Main File-17230-7-0-20-1772078239.7z",
    "Enhanced Lights and FX-2424-3-06.7z",
    "Experience-17751-3-7-3-1762819463.7z",
    "Faction Armors and Weapons Retexture SE-169281-1-1-1-1781100129.7z",
    "FSMP 4.0.1 57339 4.0.1 2026-07-05T18-19Z n0oTCm5i9.zip",
    "Guards and Stormcloaks Armors Retexture SE-98115-1-1-1-1781098195.7z",
    "HDT-SMP Hair And Wigs - Misc Improvements 184585 1 2026-07-23T16-51Z pQ0AZ32mg.7z",
    "Imperial Armors and Weapons Retexture SE 86097 2.0.5 2026-06-24T05-27Z UK35XnPRZ.7z",
    "Iron Armors and Weapons Retexture SE-84978-2-1-1-1774540708.7z",
    "LeanWolfs Better-Shaped Weapons Installer v2.1.03 SE-2017-2-1-03-1585765834.7z",
    "Leather Armors Retexture SE-90153-2-0-1-1781096026.7z",
    "Papyrus Extender 22854 6.4.3 2026-07-20T04-04Z OyYrPubih.7z",
    "Particle Patch 65720 1.4.5 2026-08-04T07-34Z gbrZ4tLRV.zip",
    "Static Skill Leveling Rewriten-89940-1-8-1716997976.7z",
    "Steel Armors and Weapons Retexture SE-85445-2-1-2-1781041196.7z",
    "Unique Armors and Weapons Retexture SE-105771-1-4-0-1767736934.7z",
    "Xavbio Armors Collection - HIMBO V5 Refits 131449 2.4.3 2026-07-16T15-12Z FpnkHZPNH.7z",
]


def _parse_config_safe(path: Path):
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16" if raw.startswith(b"\xff\xfe") else "utf-16-be"
        return ET.fromstring(raw.decode(encoding))
    return _parse_config(path)


def _selective_extract(archive: Path):
    if not archive.name.casefold().endswith(".7z"):
        return extract_to_temp(archive)
    records = list_archive(archive)
    members = [str(item["name"]) for item in records]
    normalized = {name.replace("\\", "/").casefold(): name for name in members}
    config_key = next(key for key in normalized if key.endswith("/moduleconfig.xml") or key == "moduleconfig.xml")
    config_member = normalized[config_key]
    temporary = Path(tempfile.mkdtemp(prefix="mo2cli-backfill-"))
    with py7zr.SevenZipFile(archive, "r") as seven_zip:
        seven_zip.extract(path=temporary, targets=[config_member])
    config = next(temporary.rglob("ModuleConfig.xml"))
    root = _parse_config_safe(config)
    base_prefix = config.parent.parent.relative_to(temporary).as_posix().strip(".").strip("/")
    targets = {config_member}
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].casefold() not in {"file", "folder"}:
            continue
        source = node.attrib.get("source", "").replace("\\", "/").strip("/")
        if not source:
            continue
        prefix = f"{base_prefix}/{source}" if base_prefix else source
        prefix = prefix.strip("/").casefold()
        if node.tag.rsplit("}", 1)[-1].casefold() == "folder":
            targets.update(actual for key, actual in normalized.items() if key == prefix or key.startswith(prefix + "/"))
        elif prefix in normalized:
            targets.add(normalized[prefix])
    try:
        with py7zr.SevenZipFile(archive, "r") as seven_zip:
            seven_zip.extract(path=temporary, targets=sorted(targets))
    except Exception:
        winrar = Path(r"C:\Program Files\WinRAR\WinRAR.exe")
        if not winrar.is_file():
            raise
        # Compatibility fallback for 7z archives using BCJ2, unsupported by py7zr.
        subprocess.run(
            [str(winrar), "x", "-ibck", "-inul", "-y", str(archive), str(temporary) + "\\"],
            check=True,
        )
    # Normalize malformed UTF-16 files whose XML declaration incorrectly says UTF-8;
    # the shared FOMOD planner expects a declaration matching the actual bytes.
    config = next(temporary.rglob("ModuleConfig.xml"))
    config.write_bytes(ET.tostring(_parse_config_safe(config), encoding="utf-8", xml_declaration=True))

    class Extracted:
        root = temporary

        @staticmethod
        def close() -> None:
            # The temporary directory is intentionally left for post-run audit.
            return None

    return Extracted()


def _groups(root: Path):
    install_steps = _first(_parse_config_safe(_find_config(root)), "installSteps")
    steps = _children(install_steps, "installStep") if install_steps is not None else []
    order = (install_steps.attrib.get("order", "Ascending") if install_steps is not None else "Ascending").casefold()
    steps.sort(key=lambda item: item.attrib.get("name", "").casefold(), reverse=order == "descending")
    step_seen: dict[str, int] = {}
    result = []
    for step in steps:
        step_name = step.attrib.get("name", "")
        step_seen[step_name] = step_seen.get(step_name, 0) + 1
        step_key = step_name if step_seen[step_name] == 1 else f"{step_name}#{step_seen[step_name]}"
        groups = _first(step, "optionalFileGroups")
        group_seen: dict[str, int] = {}
        for group in _children(groups, "group") if groups is not None else []:
            name = group.attrib.get("name", "")
            group_seen[name] = group_seen.get(name, 0) + 1
            key = f"{step_key}/{name}"
            if group_seen[name] > 1:
                key += f"#{group_seen[name]}"
            result.append((key, group))
    return result


def _option_files(base: Path, plugin):
    result = []
    for operation in _files(_first(plugin, "files")):
        source = base.joinpath(*Path(str(operation["source"])).parts)
        destination = str(operation.get("destination") or "").replace("\\", "/")
        if operation.get("folder"):
            if source.is_dir():
                result.extend((item, Path(destination) / item.relative_to(source)) for item in source.rglob("*") if item.is_file())
        else:
            target = Path(destination)
            if not destination or destination.endswith("/"):
                target /= source.name
            result.append((source, target))
    return result


def _separator(instance: Instance, profile: str, mod_name: str) -> str | None:
    entries = list(reversed(ModList.read(instance.profile_path(profile) / "modlist.txt").entries))
    index = next((i for i, entry in enumerate(entries) if entry.name.casefold() == mod_name.casefold()), None)
    if index is None:
        return None
    return next((entry.name for entry in reversed(entries[:index]) if entry.name.casefold().endswith("separator")), None)


def main(instance_path: str, profile: str) -> None:
    instance = Instance.open(instance_path)
    meta_by_file: dict[str, str] = {}
    for mod in instance.mods_dir.iterdir():
        if not mod.is_dir():
            continue
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(mod / "meta.ini", encoding="utf-8-sig")
        except Exception:
            continue
        if parser.has_section("General"):
            installation_file = parser["General"].get("installationFile", "").strip()
            if installation_file:
                meta_by_file[installation_file.casefold()] = mod.name

    states = {path.name: "Active" for path in _active_plugin_files(instance, profile).values()}
    existing = [item for item in _read(decision_path(instance, profile))["decisions"] if isinstance(item, dict)]
    known_hashes = {item.get("archive_sha256") for item in existing}
    digest_cache: dict[str, bytes] = {}

    def digest(path: Path) -> bytes:
        key = str(path)
        if key not in digest_cache:
            value = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    value.update(block)
            digest_cache[key] = value.digest()
        return digest_cache[key]

    saved = 0
    for filename in ARCHIVES:
        archive = instance.downloads_dir / filename
        mod_name = meta_by_file.get(filename.casefold())
        if not archive.is_file() or not mod_name or sha256(archive) in known_hashes:
            continue
        print(f"BACKFILL {mod_name}", flush=True)
        extracted = _selective_extract(archive)
        try:
            root = extracted.root
            base = _find_config(root).parent.parent
            mod = instance.mods_dir / mod_name
            installed = {path.relative_to(mod).as_posix().casefold(): path for path in mod.rglob("*") if path.is_file() and path.name.casefold() != "meta.ini"}
            default = plan_extracted(root, file_states=states, game_version="1.6.1170")
            selections: dict[str, list[str]] = {}
            uncertain: list[str] = []
            path_only: list[str] = []
            for key, group in _groups(root):
                plugins = _children(_first(group, "plugins"), "plugin")
                scores = []
                for plugin in plugins:
                    operations = _option_files(base, plugin)
                    exact = 0
                    present = 0
                    for source, target in operations:
                        installed_path = installed.get(target.as_posix().casefold())
                        if installed_path is not None:
                            present += 1
                            if source.is_file() and source.stat().st_size == installed_path.stat().st_size and digest(source) == digest(installed_path):
                                exact += 1
                    scores.append((plugin.attrib.get("name", ""), exact, present, len(operations), _plugin_type(plugin, FomodContext({}, states, "1.6.1170"))))
                defaults = list((default.get("selections") or {}).get(key, []))
                matched = [score for score in scores if score[1] or score[2]]
                chosen = list(defaults)
                evidence = "default"
                group_type = group.attrib.get("type", "SelectAny")
                if matched:
                    if group_type in {"SelectExactlyOne", "SelectAtMostOne"}:
                        best = max(matched, key=lambda item: (item[1], item[2], item[1] / item[3] if item[3] else 0))
                        chosen = [best[0]]
                        evidence = "hash" if best[1] else "path"
                    elif group_type == "SelectAll":
                        chosen = [item[0] for item in matched]
                        evidence = "hash" if any(item[1] for item in matched) else "path"
                    else:
                        required = [item[0] for item in scores if item[4] == "Required"]
                        chosen = list(dict.fromkeys(required + [item[0] for item in matched]))
                        evidence = "hash" if any(item[1] for item in matched) else "path"
                elif not chosen:
                    usable = [item for item in scores if item[4] != "NotUsable"]
                    if usable:
                        chosen = [usable[0][0]]
                    uncertain.append(key)
                else:
                    uncertain.append(key)
                if evidence == "path":
                    path_only.append(key)
                if chosen:
                    selections[key] = chosen

            plan = plan_extracted(root, selections=selections, file_states=states, game_version="1.6.1170")
            warnings = ["Selections were inferred retroactively by comparing installed mod files against archive sources; not a direct capture from the MO2 FOMOD screen."]
            if uncertain:
                warnings.append("Groups with no exact match or using fallback defaults: " + ", ".join(sorted(set(uncertain))))
            if path_only:
                warnings.append("Groups inferred strictly by destination path matching: " + ", ".join(sorted(set(path_only))))
            if plan.get("errors"):
                warnings.append("Plan validation warnings: " + "; ".join(str(error) for error in plan["errors"]))
            payload = _decision_payload(instance, profile, archive, config_hash(root), plan, mod_name, {}, "1.6.1170", _separator(instance, profile, mod_name), "backfilled", warnings)
            payload["backfill"] = {"method": "installed-output-comparison", "uncertain_groups": sorted(set(uncertain)), "path_only_groups": sorted(set(path_only))}
            save(instance, profile, payload)
            known_hashes.add(sha256(archive))
            saved += 1
            print(f"  saved={len(plan.get('selections', {}))} uncertain={len(set(uncertain))} errors={len(plan.get('errors', []))}", flush=True)
        finally:
            extracted.close()
    print(f"SAVED {saved}", flush=True)


if __name__ == "__main__":
    main(r"C:\Modlists\Skycoop", "Default")
