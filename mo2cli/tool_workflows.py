from __future__ import annotations

from pathlib import Path

from .formats import ModList, write_text
from .separators import ensure as ensure_separator, group as group_separator
from .tools import find_executable
from .usvfs import run as run_vfs
from .workspace import Instance, Mo2Error, _safe_name


TOOL_OUTPUTS_SEPARATOR = "18. _____________________________________ TOOL OUTPUTS __________________________________________"
PANDORA_TITLE = "Pandora Behaviour Engine+"
BODYSLIDE_TITLE = "BodySlide"


def _find_mod_dir(instance: Instance, name: str) -> Path | None:
    if not instance.mods_dir.is_dir():
        return None
    return next((item for item in instance.mods_dir.iterdir() if item.name.casefold() == name.casefold()), None)


def _output_metadata(instance: Instance, path: Path) -> None:
    if (path / "meta.ini").exists():
        return
    game_name = "SkyrimSE" if instance.game_name.casefold().startswith("skyrim special edition") else instance.game_name
    write_text(path / "meta.ini", "[General]\r\nmodid=0\r\nversion=\r\nnewestVersion=\r\ncategory=0\r\ninstallationFile=\r\ngameName=" + game_name + "\r\n")


def ensure_output_mod(instance: Instance, profile: str | None, name: str, separator: str = TOOL_OUTPUTS_SEPARATOR) -> Path:
    """Create/register an output mod without deleting an existing build."""
    _safe_name(name)
    path = _find_mod_dir(instance, name)
    if path is None:
        path = instance.mods_dir / name
        path.mkdir(parents=True, exist_ok=False)
    elif not path.is_dir():
        raise Mo2Error(f"Output mod path is not a directory: {path}")
    _output_metadata(instance, path)

    profile_name = instance.profile_name(profile)
    profile_path = instance.profiles_dir / profile_name
    modlist_path = profile_path / "modlist.txt"
    model = ModList.read(modlist_path)
    if model.find(name) is None:
        model.add(name, enabled=True)
        write_text(modlist_path, model.render())
    elif not model.find(name).enabled:
        model.set_enabled(name, True)
        write_text(modlist_path, model.render())

    ensure_separator(instance, profile_name, separator)
    group_separator(instance, profile_name, separator, [name])
    return path


def _destination(instance: Instance, destination: str | None) -> str | None:
    if destination:
        return str(Path(destination).expanduser())
    if instance.game_path and (instance.game_path / "Data").is_dir():
        return str(instance.game_path / "Data")
    return None


def _base_result(title: str, binary: Path, args: list[str], output_mod: Path, dry_run: bool) -> dict[str, object]:
    return {
        "workflow": title,
        "binary": str(binary),
        "arguments": args,
        "output_mod": str(output_mod),
        "dry_run": dry_run,
        "virtualization": "usvfs",
    }


def run_pandora(
    instance: Instance,
    profile: str | None,
    output_mod: str = "Pandora Output",
    separator: str = TOOL_OUTPUTS_SEPARATOR,
    tesv: str | None = None,
    auto_run: bool = True,
    auto_close: bool = True,
    usvfs_dir: str | None = None,
    destination: str | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    executable = find_executable(instance, PANDORA_TITLE)
    binary = Path(str(executable["binary"]))
    output_path = instance.mods_dir / output_mod
    args: list[str] = ["--output", str(output_path)]
    if tesv:
        args.extend(["--tesv", str(Path(tesv).expanduser())])
    if auto_run:
        args.append("--auto_run")
    if auto_close:
        args.append("--auto_close")
    result = _base_result("pandora", binary, args, output_path, dry_run)
    result["auto_run"] = auto_run
    result["auto_close"] = auto_close
    if dry_run:
        result["destination"] = _destination(instance, destination)
        return result
    ensure_output_mod(instance, profile, output_mod, separator)
    result.update(run_vfs(instance, profile, str(binary), args, usvfs_dir, _destination(instance, destination), direct_mods=True))
    return result


def run_bodyslide(
    instance: Instance,
    profile: str | None,
    preset: str,
    groups: list[str],
    output_mod: str = "BodySlide Output",
    separator: str = TOOL_OUTPUTS_SEPARATOR,
    trimorphs: bool = False,
    usvfs_dir: str | None = None,
    destination: str | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    if not preset.strip():
        raise Mo2Error("BodySlide preset cannot be empty.")
    cleaned_groups = [item.strip() for item in groups if item.strip()]
    if not cleaned_groups:
        raise Mo2Error("At least one --group is required for BodySlide.")
    executable = find_executable(instance, BODYSLIDE_TITLE)
    binary = Path(str(executable["binary"]))
    output_path = instance.mods_dir / output_mod
    args: list[str] = ["--groupbuild", ",".join(cleaned_groups), "--targetdir", str(output_path), "--preset", preset]
    if trimorphs:
        args.append("--trimorphs")
    result = _base_result("bodyslide", binary, args, output_path, dry_run)
    result["preset"] = preset
    result["groups"] = cleaned_groups
    result["trimorphs"] = trimorphs
    if dry_run:
        result["destination"] = _destination(instance, destination)
        return result
    ensure_output_mod(instance, profile, output_mod, separator)
    result.update(run_vfs(instance, profile, str(binary), args, usvfs_dir, _destination(instance, destination), direct_mods=True))
    return result
