from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .archives import archive_stem, extract_to_directory, list_archive, sha256
from .downloads import audit_downloads, set_meta
from .downloads import fetch as fetch_download
from .downloads import list_downloads
from .fomod import inspect_archive as inspect_fomod
from .fomod import load_fomod_plus_record
from .fomod_decisions import list_decisions
from .formats import ModList, PluginList, write_text
from .game import run_game
from .github import fetch_asset as fetch_github_asset
from .github import is_github_ref
from .inis import diff_game_ini, get_value, list_inis, set_value
from .installer import install_archive, remove_mod, rename_mod
from .instance import initialize, initialize_portable
from .instances import list_instances
from .journal import history as journal_history
from .journal import undo_last
from .manifest import apply as apply_manifest
from .metadata import sync_metadata
from . import mo2plugins
from .nexus import download_reference
from .plugins import analyze as analyze_plugins
from .plugins import ordered_entries as ordered_plugin_entries
from .plugins import remove_plugin_entries, sync_plugin_lists
from .profiles import export_profile, import_profile
from .safety import mutation_guard
from .separators import apply_list as apply_separator_list
from .separators import create as create_separator
from .separators import ensure as ensure_separator
from .separators import group as group_separator
from .separators import list_separators
from .separators import remove as remove_separator
from .tool_workflows import run_bodyslide, run_pandora
from .tools import register_executable, remove_executable, run_executable
from .usvfs import cleanup as cleanup_vfs
from .usvfs import run as run_vfs
from .usvfs import status as vfs_status
from .workspace import Instance, Mo2Error


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--instance", "-i", default=argparse.SUPPRESS, help="MO2 instance directory or ModOrganizer.ini")
    parser.add_argument("--profile", "-p", default=argparse.SUPPRESS, help="Target profile name")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Output as JSON")
    parser.add_argument("--non-interactive", action="store_true", default=argparse.SUPPRESS, help="Never prompt (also MO2CLI_NONINTERACTIVE=1); FOMOD reuse 'auto' then behaves like a run without a terminal")


def _output(value, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    elif isinstance(value, list):
        for item in value:
            print(item if isinstance(item, str) else " ".join(f"{k}={v}" for k, v in item.items()))
    elif isinstance(value, dict):
        for key, item in value.items():
            print(f"{key}: {item}")
    else:
        print(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mo2", description="A safe, scriptable CLI for Mod Organizer 2")
    parser.add_argument("--version", action="version", version=__version__)
    _common(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    info = sub.add_parser("info", help="Show instance and selected profile summary")
    _common(info)

    profiles = sub.add_parser("profiles", help="Manage profiles")
    _common(profiles)
    ps = profiles.add_subparsers(dest="profiles_command", required=True)
    ps.add_parser("list", help="List profiles")
    create = ps.add_parser("create", help="Create profile")
    create.add_argument("name")
    create.add_argument("--from", dest="source")
    use = ps.add_parser("use", help="Switch active profile")
    use.add_argument("name")
    delete = ps.add_parser("delete", help="Delete profile")
    delete.add_argument("name")
    delete.add_argument("--yes", action="store_true", help="Confirm profile deletion")
    export_command = ps.add_parser("export", help="Export profile to ZIP archive")
    export_command.add_argument("output")
    import_command = ps.add_parser("import", help="Import profile from ZIP archive")
    import_command.add_argument("archive")
    import_command.add_argument("--name")
    import_command.add_argument("--replace", action="store_true")
    profile_settings = ps.add_parser("settings", help="Inspect or change profile local INI/save settings")
    profile_settings.add_argument("--local-inis", choices=("on", "off"))
    profile_settings.add_argument("--local-saves", choices=("on", "off"))

    mods = sub.add_parser("mods", help="Inspect and manage mods")
    _common(mods)
    ms = mods.add_subparsers(dest="mods_command", required=True)
    mod_list = ms.add_parser("list")
    mod_list.add_argument("--all", action="store_true", help="Include installed mods not listed in profile")
    show = ms.add_parser("show", help="Show mod metadata and file summary")
    show.add_argument("name")
    metadata = ms.add_parser("metadata", help="Read or update mod metadata")
    metadata.add_argument("name", nargs="?")
    metadata.add_argument("--set", dest="updates", action="append", metavar="KEY=VALUE")
    metadata.add_argument("--sync", action="store_true", help="Sync mod metadata from download sidecars (*.meta)")
    metadata.add_argument("--all", action="store_true", help="Include mods outside profile during sync")
    fomod = ms.add_parser("fomod", help="Inspect profile FOMOD decision records")
    fomod_sub = fomod.add_subparsers(dest="fomod_command", required=True)
    fomod_sub.add_parser("decisions", help="List saved FOMOD decisions for this profile")
    install = ms.add_parser("install", help="Install simple mod or FOMOD from archive")
    install.add_argument("archive")
    install.add_argument("--name")
    install.add_argument("--disabled", action="store_true")
    install.add_argument("--replace", action="store_true")
    install.add_argument("--merge", action="store_true", help="Merge files into existing mod folder instead of replacing")
    install.add_argument("--allow-fomod", action="store_true")
    install.add_argument("--fomod-select", action="append", metavar="GROUP=PLUGIN[,PLUGIN...]")
    install.add_argument("--fomod-flag", action="append", metavar="KEY=VALUE")
    install.add_argument("--fomod-game-version")
    install.add_argument("--fomod-reuse", choices=("auto", "always", "never"), default="auto", help="Policy for reusing saved FOMOD decisions")
    install.add_argument("--dry-run", action="store_true")
    install.add_argument("--source", help="Source subfolder inside archive")
    install.add_argument("--nexus-api-key")
    install.add_argument("--game")
    install.add_argument("--file", dest="file_name")
    install.add_argument("--download-name")
    install.add_argument("--sha256")
    install.add_argument("--separator")
    install.add_argument("--asset", help="Release asset glob when ARCHIVE is github:owner/repo@latest|TAG")
    install.add_argument("--auto-download", "--auto", action="store_true", help="Automatically bypass 5s timer via browser for free accounts")
    remove = ms.add_parser("remove", help="Move mod to recoverable trash")
    remove.add_argument("name")
    remove.add_argument("--yes", action="store_true")
    remove.add_argument("--purge", action="store_true", help="Permanently delete instead of moving to trash")
    rename = ms.add_parser("rename", help="Rename mod across all profiles")
    rename.add_argument("old")
    rename.add_argument("new")
    for name in ("enable", "disable"):
        command = ms.add_parser(name)
        command.add_argument("name", nargs="+")
    move = ms.add_parser("move")
    move.add_argument("name")
    move.add_argument("target", type=int, help="Visible 0-based index in MO2 left panel")
    conflicts = ms.add_parser("conflicts")
    conflicts.add_argument("path", nargs="?")
    separator = ms.add_parser("separator", help="Create MO2 separators and group mods")
    ss = separator.add_subparsers(dest="separator_command", required=True)
    separator_list = ss.add_parser("list")
    separator_create = ss.add_parser("create")
    separator_create.add_argument("name")
    separator_create.add_argument("--before")
    separator_create.add_argument("--after")
    separator_create.add_argument("--all-profiles", action="store_true")
    separator_create.add_argument("--color", help="Separator colour as #RRGGBB (stored as a QColor variant in meta.ini)")
    separator_apply = ss.add_parser("apply", help="Create/colour/order separators from a list file (idempotent)")
    separator_apply.add_argument("listfile", help="One 'Name' or 'Name | #RRGGBB' per line, top to bottom as shown in MO2")
    separator_apply.add_argument("--dry-run", action="store_true")
    separator_remove = ss.add_parser("remove")
    separator_remove.add_argument("name")
    separator_remove.add_argument("--yes", action="store_true")
    separator_remove.add_argument("--purge", action="store_true")
    separator_group = ss.add_parser("group", help="Move mods directly under separator")
    separator_group.add_argument("separator")
    separator_group.add_argument("name", nargs="+")

    plugins = sub.add_parser("plugins", help="Inspect and manage plugins")
    _common(plugins)
    pls = plugins.add_subparsers(dest="plugins_command", required=True)
    remove_plugins = pls.add_parser("remove", help="Remove stale plugin names from profile lists")
    remove_plugins.add_argument("name", nargs="+")
    pls.add_parser("list")
    for name in ("enable", "disable"):
        command = pls.add_parser(name)
        command.add_argument("name", nargs="+")
    pmove = pls.add_parser("move")
    pmove.add_argument("name")
    pmove.add_argument("target", type=int)
    pls.add_parser("check", help="Check plugin files and master dependencies")
    pls.add_parser("sync", help="Add plugin files from active mods to profile lists")
    sort_plugins = pls.add_parser("sort", help="Sort plugins using LOOT CLI")
    sort_plugins.add_argument("--loot-exe")
    sort_plugins.add_argument("--dry-run", action="store_true")

    owner = sub.add_parser("file-owner", help="Find active mod providers for a virtual file")
    _common(owner)
    owner.add_argument("path")
    doctor = sub.add_parser("doctor", help="Run profile consistency checks")
    _common(doctor)
    doctor.add_argument("--deep", action="store_true", help="Include plugin header/master validation")
    snapshot = sub.add_parser("snapshot", help="Export JSON snapshot of profile")
    _common(snapshot)
    snapshot.add_argument("-o", "--output")

    files = sub.add_parser("files", help="Inspect virtual file tree")
    _common(files)
    fs = files.add_subparsers(dest="files_command", required=True)
    file_list = fs.add_parser("list")
    file_list.add_argument("path", nargs="?")
    file_list.add_argument("--all-providers", action="store_true")
    materialize = fs.add_parser("materialize", help="Export virtual file tree to a physical folder")
    materialize.add_argument("destination")
    materialize.add_argument("--replace", action="store_true")

    archives = sub.add_parser("archive", help="Inspect and hash archive files")
    _common(archives)
    archives.add_argument("archive_command", choices=("list", "hash", "info", "fomod", "extract"))
    archives.add_argument("path")
    archives.add_argument("--to", dest="extract_to", help="extract: destination directory")
    archives.add_argument("--strip-root", action="store_true", help="extract: drop the single top-level folder that wraps the archive")
    archives.add_argument("--replace", action="store_true", help="extract: allow a non-empty destination; colliding files are overwritten, nothing is deleted")
    archives.add_argument("--dry-run", action="store_true", help="extract: validate and report without writing")
    archives.add_argument("--fomod-select", action="append", metavar="GROUP=PLUGIN[,PLUGIN...]", help="Apply selections while inspecting a FOMOD")
    archives.add_argument("--fomod-flag", action="append", metavar="KEY=VALUE", help="Seed a FOMOD condition flag")
    archives.add_argument("--fomod-game-version", help="Override the game version detected from the MO2 instance")
    archives.add_argument("--fomod-plus-db", help="Replay and compare a FOMOD Plus JSON database record")
    archives.add_argument("--fomod-plus-name", help="FOMOD Plus display name or Nexus mod ID")

    inis = sub.add_parser("inis", help="Inspect and edit profile INI/CFG files")
    _common(inis)
    ins = inis.add_subparsers(dest="inis_command", required=True)
    ins.add_parser("list")
    get_ini = ins.add_parser("get")
    get_ini.add_argument("file")
    get_ini.add_argument("section")
    get_ini.add_argument("key")
    set_ini = ins.add_parser("set")
    set_ini.add_argument("file")
    set_ini.add_argument("section")
    set_ini.add_argument("key")
    set_ini.add_argument("value")
    diff_ini = ins.add_parser("diff", help="Diff profile INI against game INI")
    diff_ini.add_argument("file")

    settings = sub.add_parser("settings", help="Read and edit the instance's ModOrganizer.ini (not profile INIs)")
    _common(settings)
    sts = settings.add_subparsers(dest="settings_command", required=True)
    settings_list = sts.add_parser("list", help="List sections, or the keys of one section")
    settings_list.add_argument("section", nargs="?")
    settings_list.add_argument("--raw", action="store_true", help="Show stored values without QSettings decoding")
    settings_get = sts.add_parser("get", help="Read one value (use '-' as SECTION for keys before the first header)")
    settings_get.add_argument("section")
    settings_get.add_argument("key")
    settings_get.add_argument("--raw", action="store_true", help="Show the stored value without QSettings decoding")
    settings_set = sts.add_parser("set", help="Write one value; only that line changes")
    settings_set.add_argument("section")
    settings_set.add_argument("key")
    settings_set.add_argument("value")
    settings_set.add_argument("--raw", action="store_true", help="Write VALUE literally, with no QSettings escaping")
    settings_set.add_argument("--bytearray", action="store_true", help="Store as @ByteArray(...) like MO2 does for paths and profile names")
    settings_set.add_argument("--dry-run", action="store_true")

    conflicts = sub.add_parser("conflicts", help="File level conflict reports across enabled mods")
    _common(conflicts)
    cfs = conflicts.add_subparsers(dest="conflicts_command", required=True)
    conflicts_report = cfs.add_parser("report", help="Per path winner and losers plus per mod overwrite counts")
    conflicts_report.add_argument("--mod", help="Only conflicts that involve this mod")
    conflicts_report.add_argument("--ext", help="Comma separated extensions, e.g. .esp,.pex,.dds")
    conflicts_report.add_argument("--include-bsa", action="store_true", help="Also read file lists of BSAs attached to enabled plugins")
    cfs.add_parser("redundant", help="Enabled mods that are empty or fully overwritten by higher priority mods")

    stockgame = sub.add_parser("stockgame", help="Create and verify a Wabbajack style Stock Game folder")
    _common(stockgame)
    sgs = stockgame.add_subparsers(dest="stockgame_command", required=True)
    sg_create = sgs.add_parser("create", help="Copy vanilla game files from a Steam folder (read only) into <instance>/Stock Game")
    sg_create.add_argument("--from", dest="source", required=True, help="Game folder containing SkyrimSE.exe; never modified")
    sg_create.add_argument("--to", help="Destination (default: <instance>/Stock Game)")
    sg_create.add_argument("--expect-version", help="Required SkyrimSE.exe file version, e.g. 1.6.1170.0")
    sg_create.add_argument("--allow-mismatch", action="store_true", help="Copy even when the version differs from --expect-version")
    sg_create.add_argument("--dry-run", action="store_true")
    sg_verify = sgs.add_parser("verify", help="Print SkyrimSE.exe version, size and hashes (xxHash64 and SHA-256)")
    sg_verify.add_argument("--dir", help="Game folder (default: <instance>/Stock Game)")
    sg_verify.add_argument("--expect-version")

    tools = sub.add_parser("tools", help="Manage and run MO2 custom executables")
    _common(tools)
    tls = tools.add_subparsers(dest="tools_command", required=True)
    tls.add_parser("list")
    tool_register = tls.add_parser("register", help="Register or update an MO2 custom executable")
    tool_register.add_argument("title")
    tool_register.add_argument("binary")
    tool_register.add_argument("--working-directory")
    tool_register.add_argument("--arguments", default="")
    tool_register.add_argument("--replace", action="store_true")
    tool_remove = tls.add_parser("remove", help="Remove an MO2 custom executable by index or unique title")
    tool_remove.add_argument("target")
    tool_run = tls.add_parser("run", help="Run executable directly without USVFS injection")
    tool_run.add_argument("title")
    tool_run.add_argument("extra", nargs="*")
    tool_run.add_argument("--wait", action="store_true")
    xedit_clean = tls.add_parser("xedit-clean", help="Quick auto clean one plugin with SSEEdit (-qac) inside USVFS and parse the log")
    xedit_clean.add_argument("plugin")
    xedit_clean.add_argument("--exe", help="SSEEdit executable (default: a registered SSEEdit/xEdit tool)")
    xedit_clean.add_argument("--log-dir", help="Directory for -R: (default: <base>/Tools/xedit-logs)")
    xedit_clean.add_argument("--capture-to", metavar="MOD", help="Move files that appeared in overwrite into this mod (created if missing)")
    xedit_clean.add_argument("--usvfs-dir")
    xedit_clean.add_argument("--moprofile", action="store_true", help="Also pass -moprofile:<profile>")
    xedit_clean.add_argument("--force", action="store_true", help="Allow cleaning vanilla/Creation Club plugins")
    xedit_clean.add_argument("--dry-run", action="store_true")
    loot_sort = tls.add_parser("loot-sort", help="Sort with lootcli into a temporary file; --apply writes loadorder.txt and plugins.txt")
    loot_sort.add_argument("--exe", help="lootcli.exe (default: registered tool or <instance>/loot/lootcli.exe)")
    loot_sort.add_argument("--game", help="lootcli --game value (default SkyrimSE)")
    loot_sort.add_argument("--apply", action="store_true", help="Write the sorted order (journaled, undoable)")
    loot_sort.add_argument("--update-masterlist", action="store_true", help="Let lootcli download the masterlist (default: --skipUpdateMasterlist)")
    loot_sort.add_argument("--dry-run", action="store_true")
    bsarch_command = tls.add_parser("bsarch", help="Pack, unpack or list BSA archives with BSArch")
    bsarch_command.add_argument("action", choices=("pack", "unpack", "list"))
    bsarch_command.add_argument("first", help="pack: source folder; unpack/list: archive")
    bsarch_command.add_argument("second", nargs="?", help="pack: archive to create; unpack: destination folder")
    bsarch_command.add_argument("--exe", help="BSArch executable (default: a registered BSArch tool)")
    bsarch_command.add_argument("--format", dest="fmt", default="sse", help="BSArch game switch without dash (sse, tes5, fo4, ...)")
    bsarch_command.add_argument("--no-compress", action="store_true")
    bsarch_command.add_argument("--no-mt", action="store_true")
    bsarch_command.add_argument("--share", action="store_true", help="-share:yes (share identical files)")
    bsarch_command.add_argument("--filter", action="append", metavar="MASK", help="File mask such as *.nif (repeatable)")
    bsarch_command.add_argument("--capture-to", metavar="MOD", help="Move files that appeared in overwrite into this mod")
    bsarch_command.add_argument("--dry-run", action="store_true")
    synth = tls.add_parser("synthesis-run", help="Run 'Synthesis.CLI run-pipeline' inside USVFS; output goes to mods/<output mod>")
    synth.add_argument("--settings", required=True, help="PipelineSettings.json (-s)")
    synth.add_argument("--output-mod", default="Synthesis Output", help="Mod folder used as -o (default: Synthesis Output)")
    synth.add_argument("--exe", help="Synthesis.CLI.exe (default: a registered Synthesis.CLI tool)")
    synth.add_argument("--profile-id", help="Synthesis profile identifier (-p)")
    synth.add_argument("--data-folder", help="-d (default: game Data folder)")
    synth.add_argument("--load-order", help="-l (default: the profile's plugins.txt)")
    synth.add_argument("--extra-data", help="-e ExtraDataFolder")
    synth.add_argument("--target-runtime", help="-t, e.g. net8.0")
    synth.add_argument("--capture-to", metavar="MOD", help="Move files that appeared in overwrite into this mod (created if missing)")
    synth.add_argument("--usvfs-dir")
    synth.add_argument("--dry-run", action="store_true")
    pandora_run = tls.add_parser("pandora-run", help="Run Pandora Behaviour Engine+ headless (--tesv/-o/--auto_run/--auto_close) inside USVFS")
    pandora_run.add_argument("--output-mod", default="Pandora Output")
    pandora_run.add_argument("--exe", help="Pandora executable (default: a registered Pandora tool)")
    pandora_run.add_argument("--tesv", help="Game root folder (default: instance gamePath)")
    pandora_run.add_argument("--no-auto-run", action="store_true")
    pandora_run.add_argument("--no-auto-close", action="store_true")
    pandora_run.add_argument("--capture-to", metavar="MOD", help="Move files that appeared in overwrite into this mod (created if missing)")
    pandora_run.add_argument("--usvfs-dir")
    pandora_run.add_argument("--dry-run", action="store_true")
    automate = tls.add_parser("automate", help="Run Pandora or BodySlide workflows with USVFS")
    automations = automate.add_subparsers(dest="automation", required=True)
    pandora = automations.add_parser("pandora", help="Run Pandora with saved patch selections")
    pandora.add_argument("--output-mod", default="Pandora Output")
    pandora.add_argument("--separator", help="Optional existing/new separator for the output mod")
    pandora.add_argument("--tesv", help="Skyrim root folder for Pandora")
    pandora.add_argument("--no-auto-run", action="store_true", help="Do not automatically run Pandora saved selections")
    pandora.add_argument("--no-auto-close", action="store_true", help="Do not close Pandora after execution")
    pandora.add_argument("--usvfs-dir")
    pandora.add_argument("--destination", help="USVFS virtual Data destination")
    pandora.add_argument("--dry-run", action="store_true")
    bodyslide = automations.add_parser("bodyslide", help="Run BodySlide batch build")
    bodyslide.add_argument("--preset", required=True, help="BodySlide preset name or XML path")
    bodyslide.add_argument("--group", action="append", required=True, help="BodySlide group to build; can be repeated")
    bodyslide.add_argument("--output-mod", default="BodySlide Output")
    bodyslide.add_argument("--separator", help="Optional existing/new separator for the output mod")
    bodyslide.add_argument("--trimorphs", action="store_true")
    bodyslide.add_argument("--usvfs-dir")
    bodyslide.add_argument("--destination", help="USVFS virtual Data destination")
    bodyslide.add_argument("--dry-run", action="store_true")

    downloads = sub.add_parser("downloads", help="Inspect MO2 downloads directory")
    _common(downloads)
    dls = downloads.add_subparsers(dest="downloads_command", required=True)
    download_list = dls.add_parser("list")
    download_list.add_argument("--hash", action="store_true")
    download_sync = dls.add_parser("sync", help="Synchronize downloads installed status with installed mods, tools, and plugins")
    download_mark = dls.add_parser("mark-installed", help="Mark download archive(s) as installed or uninstalled")
    download_mark.add_argument("archive", nargs="+", help="Archive filename(s)")
    download_mark.add_argument("--uninstalled", action="store_true", help="Mark as uninstalled instead")
    download_audit = dls.add_parser("audit", help="List archives Wabbajack could not re-download and mods whose archive is missing")
    download_meta = dls.add_parser("meta", help="Create or update an archive's .meta (Wabbajack source + MO2 state)")
    download_meta.add_argument("archive", help="Archive file name in the downloads folder, or a path")
    meta_source = download_meta.add_mutually_exclusive_group()
    meta_source.add_argument("--direct-url", help="Wabbajack directURL (GitHub release asset, CDN, ...)")
    meta_source.add_argument("--manual-url", help="Wabbajack manualURL (for example a LoversLab file page)")
    meta_source.add_argument("--nexus", metavar="MODID:FILEID", help="Nexus source: writes gameName, modID, fileID")
    download_meta.add_argument("--header", action="append", metavar="'K: V'", help="Request header for --direct-url (repeatable; stored as directURLHeaders)")
    download_meta.add_argument("--prompt", help="Text shown to the user for --manual-url")
    download_meta.add_argument("--game-name", help="MO2 game short name for --nexus (default: derived from the instance, else SkyrimSE)")
    meta_state = download_meta.add_mutually_exclusive_group()
    meta_state.add_argument("--installed", dest="meta_installed", action="store_const", const=True, default=None)
    meta_state.add_argument("--not-installed", dest="meta_installed", action="store_const", const=False)
    download_meta.add_argument("--dry-run", action="store_true")
    download_fetch = dls.add_parser("fetch", help="Download HTTP(S) or NXM URL into downloads folder")
    download_fetch.add_argument("url")
    download_fetch.add_argument("--output")
    download_fetch.add_argument("--replace", action="store_true")
    download_fetch.add_argument("--nexus-api-key")
    download_fetch.add_argument("--game")
    download_fetch.add_argument("--file", dest="file_name")
    download_fetch.add_argument("--sha256")
    download_fetch.add_argument("--asset", help="Release asset glob when URL is github:owner/repo@latest|TAG")
    download_fetch.add_argument("--header", action="append", metavar="'K: V'", help="Extra request header (repeatable); recorded as directURLHeaders")
    download_fetch.add_argument("--auto-download", "--auto", action="store_true", help="Automatically bypass 5s timer via browser for free accounts")

    mo2plugin = sub.add_parser("mo2plugin", help="Install, list and remove MO2 plugins (Root Builder, FOMOD Plus, ...) in <base>/plugins")
    _common(mo2plugin)
    mps = mo2plugin.add_subparsers(dest="mo2plugin_command", required=True)
    mp_install = mps.add_parser("install", help="Extract a plugin archive into the MO2 plugins folder (journaled, undoable)")
    mp_install.add_argument("archive")
    mp_install.add_argument("--name", help="Plugin name for list/remove (default: archive or plugin folder name)")
    mp_install.add_argument("--source", help="Archive subfolder to treat as the archive root")
    mp_install.add_argument("--replace", action="store_true", help="Reinstall; colliding files go to the trash")
    mp_install.add_argument("--direct-url", help="Wabbajack directURL to record in the archive's .meta")
    mp_install.add_argument("--dry-run", action="store_true")
    mps.add_parser("list", help="List plugins installed by mo2cli")
    mp_remove = mps.add_parser("remove", help="Move a managed plugin's files to the recoverable trash")
    mp_remove.add_argument("name")
    mp_remove.add_argument("--dry-run", action="store_true")

    manifest = sub.add_parser("manifest", help="Batch download, install, and arrange separators via manifest")
    _common(manifest)
    manifest_commands = manifest.add_subparsers(dest="manifest_command", required=True)
    manifest_apply = manifest_commands.add_parser("apply", help="Apply JSON/TOML manifest file")
    manifest_apply.add_argument("path")
    manifest_apply.add_argument("--nexus-api-key")
    manifest_apply.add_argument("--dry-run", action="store_true")
    manifest_apply.add_argument("--replace", action="store_true")
    manifest_apply.add_argument("--continue-on-error", action="store_true")
    manifest_apply.add_argument("--auto-download", "--auto", action="store_true", help="Automatically bypass 5s timer via browser for free accounts")

    nexus = sub.add_parser("nexus", help="Nexus Mods account and automation utilities")
    _common(nexus)
    nexus_sub = nexus.add_subparsers(dest="nexus_command", required=True)
    nexus_sub.add_parser("login", help="Log in interactively to Nexus Mods to save session")
    nexus_files = nexus_sub.add_parser("files", help="List files for a Nexus mod ID")
    nexus_files.add_argument("mod_id", type=int)
    nexus_files.add_argument("--game")
    nexus_files.add_argument("--nexus-api-key")
    nexus_files.add_argument("--all", action="store_true", help="Include archived, old and deleted files (Nexus GraphQL v2)")
    nexus_files.add_argument("--game-id", type=int, help="Numeric Nexus game id for --all (skyrimspecialedition is 1704)")

    instance = sub.add_parser("instance", help="Initialize a data-only MO2 instance")
    _common(instance)
    ins_instance = instance.add_subparsers(dest="instance_command", required=True)
    init_instance = ins_instance.add_parser("init")
    init_instance.add_argument("path")
    init_instance.add_argument("--game-name", required=True)
    init_instance.add_argument("--game-path", required=True)
    init_instance.add_argument("--profile", default="Default")
    init_instance.add_argument("--force", action="store_true")
    init_instance.add_argument("--portable", action="store_true", help="Build a real portable MO2 from --mo2-archive (portable.txt, ModOrganizer.ini, downloads, profile)")
    init_instance.add_argument("--mo2-archive", help="Official Mod.Organizer-X.Y.Z.7z (used with --portable; also copied into downloads)")
    init_instance.add_argument("--mo2-url", help="With --portable: Wabbajack directURL for the MO2 archive's .meta (GitHub release asset)")
    init_instance.add_argument("--my-games", help="With --portable: Documents/My Games/<game> folder to take skyrimprefs.ini/skyrimcustom.ini from")
    init_instance.add_argument("--dry-run", action="store_true", help="With --portable: report the plan without writing")

    game = sub.add_parser("game", help="Launch game directly")
    _common(game)
    games = game.add_subparsers(dest="game_command", required=True)
    game_run = games.add_parser("run", help="Launch game binary without USVFS injection")
    game_run.add_argument("--binary")
    game_run.add_argument("extra", nargs="*")
    game_run.add_argument("--wait", action="store_true")

    vfs = sub.add_parser("vfs", help="USVFS virtual execution")
    _common(vfs)
    vfs_commands = vfs.add_subparsers(dest="vfs_command", required=True)
    vfs_status_command = vfs_commands.add_parser("status")
    vfs_status_command.add_argument("--usvfs-dir")
    vfs_cleanup_command = vfs_commands.add_parser("cleanup")
    vfs_cleanup_command.add_argument("--yes", action="store_true")
    vfs_run_command = vfs_commands.add_parser("run", help="Run program with virtual mod tree; waits until exit")
    vfs_run_command.add_argument("binary")
    vfs_run_command.add_argument("extra", nargs="*")
    vfs_run_command.add_argument("--usvfs-dir")
    vfs_run_command.add_argument("--destination")

    instances = sub.add_parser("instances", help="List global MO2 instances on Windows")
    _common(instances)
    instances.add_argument("instances_command", choices=("list",))
    undo = sub.add_parser("undo")
    _common(undo)
    history = sub.add_parser("history")
    _common(history)
    history.add_argument("--limit", type=int, default=20)
    return parser


def _names(args, instance: Instance, kind: str):
    path = instance.profile_path(args.profile)
    model = ModList.read(path / "modlist.txt") if kind == "mods" else PluginList.read(path / "plugins.txt")
    return path, model


def _mutate_list(args, instance: Instance, kind: str) -> None:
    path = instance.profile_path(args.profile)
    command = args.mods_command if kind == "mods" else args.plugins_command
    if kind == "plugins" and command == "move":
        file = path / "loadorder.txt"
        model = PluginList.read(file)
    else:
        file = path / ("modlist.txt" if kind == "mods" else "plugins.txt")
        model = ModList.read(file) if kind == "mods" else PluginList.read(file)
    if command in {"enable", "disable"}:
        for name in args.name:
            try:
                model.set_enabled(name, command == "enable")
            except KeyError:
                raise Mo2Error(f"{kind[:-1].capitalize()} not found: {name}")
    elif command == "move":
        try:
            model.move_ui(args.name, args.target) if kind == "mods" else model.move(args.name, args.target)
        except KeyError:
            raise Mo2Error(f"{kind[:-1].capitalize()} not found: {args.name}")
        except IndexError:
            raise Mo2Error(f"Target index out of range: {args.target}")
    write_text(file, model.render())
    if kind == "plugins" and command == "move":
        # SSE reads order as well as activation from plugins.txt. Keep the
        # subset recorded there in loadorder.txt order without adding forced
        # DLC entries or changing any enabled flags.
        plugins_file = path / "plugins.txt"
        plugins = PluginList.read(plugins_file)
        rank = {entry.name.casefold(): index for index, entry in enumerate(model.entries)}
        ordered = iter(sorted(
            (entry for entry in plugins.entries if entry.name.casefold() in rank),
            key=lambda entry: rank[entry.name.casefold()],
        ))
        plugins.lines = [
            next(ordered) if not isinstance(line, str) and line.name.casefold() in rank else line
            for line in plugins.lines
        ]
        write_text(plugins_file, plugins.render())
    print(f"Written: {file}")


def _parse_metadata_updates(values: list[str] | None) -> dict[str, object]:
    updates: dict[str, object] = {}
    for value in values or []:
        if "=" not in value:
            raise Mo2Error(f"Metadata value must be KEY=VALUE: {value}")
        key, raw = value.split("=", 1)
        key = key.strip()
        if not key:
            raise Mo2Error("Metadata key cannot be empty.")
        if raw.casefold() in {"true", "false"}:
            parsed: object = raw.casefold() == "true"
        else:
            try:
                parsed = int(raw)
            except ValueError:
                parsed = raw
        updates[key] = parsed
    return updates


def _parse_fomod_selections(values: list[str] | None) -> dict[str, list[str]]:
    selections: dict[str, list[str]] = {}
    for value in values or []:
        if "=" not in value:
            raise Mo2Error(f"FOMOD selection must be GROUP=PLUGIN[,PLUGIN...]: {value}")
        group, raw_plugins = value.split("=", 1)
        group = group.strip()
        if not group:
            raise Mo2Error(f"FOMOD selection cannot be empty: {value}")
        if raw_plugins.strip().casefold() in {"__none__", "__empty__"}:
            selections.setdefault(group, [])
            continue
        plugins = [plugin.strip() for plugin in raw_plugins.split(",") if plugin.strip()]
        if not plugins:
            selections.setdefault(group, [])
            continue
        selections.setdefault(group, []).extend(plugins)
    return selections


def _parse_key_values(values: list[str] | None, label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values or []:
        if "=" not in value:
            raise Mo2Error(f"{label} must be KEY=VALUE: {value}")
        key, item = value.split("=", 1)
        if not key.strip():
            raise Mo2Error(f"{label} key cannot be empty.")
        result[key.strip()] = item.strip()
    return result


def _parse_headers(values: list[str] | None) -> dict[str, str] | None:
    headers: dict[str, str] = {}
    for value in values or []:
        name, separator, content = value.partition(":")
        if not separator or not name.strip():
            raise Mo2Error(f"Header must be 'Name: value': {value}")
        headers[name.strip()] = content.strip()
    return headers or None


def _resolve_archive_input(instance: Instance, args: argparse.Namespace) -> tuple[str, dict[str, object] | None]:
    source = str(args.archive)
    if args.dry_run and (source.casefold().startswith(("http://", "https://", "nxm://", "github:")) or "nexusmods.com/" in source.casefold()):
        raise Mo2Error("Remote mods are not downloaded during dry-run; plan without network access using manifest apply --dry-run.")
    if source.casefold().startswith("nxm://") or "nexusmods.com/" in source.casefold():
        download = download_reference(
            instance,
            source,
            args.nexus_api_key,
            args.game,
            args.file_name,
            args.download_name,
            args.replace,
            args.sha256,
            auto_download=getattr(args, "auto_download", False),
        )
        return str(download["path"]), download
    if is_github_ref(source):
        download = fetch_github_asset(instance, source, args.asset, args.download_name, args.replace, args.sha256)
        return str(download["path"]), download
    if source.casefold().startswith(("http://", "https://")):
        download = fetch_download(instance, source, args.download_name, args.replace, expected_sha256=args.sha256)
        return str(download["path"]), download
    return source, None


def run(args: argparse.Namespace) -> int:
    if not hasattr(args, "profile"):
        args.profile = None
    if not hasattr(args, "instance"):
        args.instance = None
    if not hasattr(args, "json"):
        args.json = False
    command = args.command
    if command == "instance":
        if args.instance_command == "init":
            if args.portable:
                if not args.mo2_archive:
                    raise Mo2Error("--portable requires --mo2-archive.")
                _output(initialize_portable(args.path, args.mo2_archive, args.game_name, args.game_path, args.profile, args.force, args.dry_run, args.mo2_url, args.my_games), args.json)
            elif args.mo2_archive or args.mo2_url or args.my_games or args.dry_run:
                raise Mo2Error("--mo2-archive, --mo2-url, --my-games and --dry-run only apply with --portable.")
            else:
                _output(initialize(args.path, args.game_name, args.game_path, args.profile, args.force), args.json)
        return 0
    if command == "game":
        if args.game_command == "run":
            _output(run_game(Instance.open(args.instance), args.binary, args.extra, args.wait), args.json)
        return 0
    if command == "archive":
        path = Path(args.path).expanduser().resolve()
        if not path.is_file():
            raise Mo2Error(f"Archive not found: {path}")
        if args.archive_command == "extract":
            if not args.extract_to:
                raise Mo2Error("archive extract requires --to DIR.")
            _output(extract_to_directory(path, Path(args.extract_to), args.strip_root, args.replace, args.dry_run), args.json)
        elif args.archive_command == "list":
            _output(list_archive(path), args.json)
        elif args.archive_command == "hash":
            print(sha256(path))
        elif args.archive_command == "fomod":
            instance_hint = args.instance or os.environ.get("MO2_INSTANCE")
            if instance_hint:
                context_instance = Instance.open(instance_hint)
            else:
                # Keep standalone archive inspection usable outside an MO2
                # directory, while honoring the same parent discovery as the
                # rest of the CLI when invoked from inside an instance.
                try:
                    context_instance = Instance.open(".")
                except Mo2Error:
                    context_instance = None
            fomod_plus_record = None
            if args.fomod_plus_db or args.fomod_plus_name:
                if not args.fomod_plus_db or not args.fomod_plus_name:
                    raise Mo2Error("--fomod-plus-db and --fomod-plus-name must be used together.")
                fomod_plus_record = load_fomod_plus_record(args.fomod_plus_db, args.fomod_plus_name)
            _output(inspect_fomod(
                path,
                instance=context_instance,
                profile=args.profile,
                selections=_parse_fomod_selections(args.fomod_select),
                flags=_parse_key_values(args.fomod_flag, "FOMOD flag"),
                game_version=args.fomod_game_version,
                fomod_plus_record=fomod_plus_record,
            ), args.json)
        else:
            entries = list_archive(path)
            _output({"path": str(path), "name": path.name, "stem": archive_stem(path), "size": path.stat().st_size, "files": len(entries)}, args.json)
        return 0
    if command == "instances":
        _output(list_instances(), args.json)
        return 0
    if command == "nexus":
        if args.nexus_command == "login":
            from .browser import interactive_login

            interactive_login()
        elif args.nexus_command == "files":
            from .nexus import NexusClient, game_domain
            instance = Instance.open(args.instance)
            client = NexusClient(args.nexus_api_key)
            game = game_domain(instance, args.game)
            files = client.files_all(game, args.mod_id, args.game_id) if args.all else client.files(game, args.mod_id)
            if args.json:
                _output(files, True)
            else:
                for f in files:
                    cat = f.get("category_name") or "OTHER"
                    print(f"category={cat} file_id={f.get('file_id')} name={f.get('name')} file_name={f.get('file_name')} version={f.get('version')}")
        return 0
    instance = Instance.open(args.instance)
    if command == "undo":
        _output(undo_last(instance), args.json)
    elif command == "history":
        _output(journal_history(instance, args.limit), args.json)
    elif command == "vfs":
        if args.vfs_command == "status":
            _output(vfs_status(instance, args.usvfs_dir), args.json)
        elif args.vfs_command == "cleanup":
            _output(cleanup_vfs(instance, args.yes), args.json)
        else:
            _output(run_vfs(instance, args.profile, args.binary, args.extra, args.usvfs_dir, args.destination), args.json)
    elif command == "info":
        _output({"instance": str(instance.root), "ini": str(instance.ini), "game": instance.game_name,
                 "game_path": str(instance.game_path) if instance.game_path else None,
                 "selected_profile": instance.selected_profile, "base": str(instance.base),
                 "mods": str(instance.mods_dir), "profiles": str(instance.profiles_dir), "downloads": str(instance.downloads_dir)}, args.json)
    elif command == "profiles":
        if args.profiles_command == "list":
            _output([{"name": name, "selected": name.casefold() == (instance.selected_profile or "").casefold()} for name in instance.list_profiles()], args.json)
        elif args.profiles_command == "create":
            path = instance.create_profile(args.name, args.source)
            _output({"created": str(path)}, args.json)
        elif args.profiles_command == "use":
            if args.name not in instance.list_profiles():
                raise Mo2Error(f"Profile not found: {args.name}")
            instance.write_selected_profile(args.name)
            print(f"Selected profile: {args.name}")
        elif args.profiles_command == "delete":
            if not args.yes:
                raise Mo2Error("Profile deletion requires --yes.")
            _output(instance.delete_profile(args.name), args.json)
        elif args.profiles_command == "export":
            result = export_profile(instance, args.profile, args.output)
            _output(result, args.json)
        elif args.profiles_command == "import":
            result = import_profile(instance, args.archive, args.name, args.replace)
            _output(result, args.json)
        elif args.profiles_command == "settings":
            result = instance.set_profile_settings(args.profile, None if args.local_inis is None else args.local_inis == "on", None if args.local_saves is None else args.local_saves == "on")
            _output(result, args.json)
    elif command in {"mods", "plugins"}:
        if command == "mods" and args.mods_command == "separator":
            if args.separator_command == "list":
                _output(list_separators(instance, args.profile), args.json)
            elif args.separator_command == "create":
                _output(create_separator(instance, args.profile, args.name, args.before, args.after, args.all_profiles, args.color), args.json)
            elif args.separator_command == "apply":
                _output(apply_separator_list(instance, args.profile, args.listfile, args.dry_run), args.json)
            elif args.separator_command == "remove":
                _output(remove_separator(instance, args.profile, args.name, args.purge, args.yes), args.json)
            else:
                _output(group_separator(instance, args.profile, args.separator, args.name), args.json)
        elif command == "mods" and args.mods_command == "list":
            path, model = _names(args, instance, "mods")
            entries = model.entries
            count = len(entries)
            data = []
            for file_index, entry in enumerate(entries):
                ui_index = count - 1 - file_index
                data.append({"index": ui_index, "ui_index": ui_index, "file_index": file_index, "name": entry.name[:-len("_separator")] if entry.name.casefold().endswith("_separator") else entry.name, "internal_name": entry.name, "enabled": entry.enabled, "foreign": entry.foreign, "separator": entry.name.casefold().endswith("_separator")})
            if args.all:
                listed = {entry.name.casefold() for entry in model.entries}
                for installed in sorted((item for item in instance.mods_dir.iterdir() if item.is_dir() and item.name.casefold() not in listed), key=lambda item: item.name.casefold()):
                    data.append({"index": None, "name": installed.name, "enabled": None, "foreign": False, "listed": False})
            _output(data, args.json)
        elif command == "mods" and args.mods_command == "show":
            path = instance.mod_path(args.name)
            metadata = instance.mod_metadata(args.name).to_dict()
            files = [file for file in path.rglob("*") if file.is_file() and file.name.casefold() != "meta.ini"]
            _output({"name": path.name, "path": str(path), "metadata": metadata, "files": len(files), "bytes": sum(file.stat().st_size for file in files)}, args.json)
        elif command == "mods" and args.mods_command == "metadata":
            if args.sync:
                if args.updates:
                    raise Mo2Error("Cannot use --sync and --set together.")
                names = [args.name] if args.name else None
                _output(sync_metadata(instance, args.profile, names, args.all), args.json)
            else:
                if not args.name:
                    raise Mo2Error("Mod name required to read metadata, or specify --sync for batch operation.")
                metadata = instance.mod_metadata(args.name)
                updates = _parse_metadata_updates(args.updates)
                if updates:
                    metadata.update(updates)
                _output(metadata.to_dict(), args.json)
        elif command == "mods" and args.mods_command == "fomod":
            if args.fomod_command == "decisions":
                _output(list_decisions(instance, args.profile), args.json)
        elif command == "mods" and args.mods_command == "install":
            archive, download = _resolve_archive_input(instance, args)
            metadata_updates = {}
            if isinstance(download, dict) and download.get("source") == "nexus":
                mod_id = download.get("mod_id")
                file_id = download.get("file_id")
                metadata_updates = {"modID": mod_id, "fileID": file_id, "version": download.get("version"), "repository": "Nexus"}
                if mod_id is not None and file_id is not None:
                    metadata_updates["url"] = f"https://www.nexusmods.com/{download.get('game', 'skyrimspecialedition')}/mods/{mod_id}?tab=files&file_id={file_id}"
                metadata_updates = {key: value for key, value in metadata_updates.items() if value is not None}
            result = install_archive(instance, archive, args.profile, args.name, args.disabled, args.replace, args.merge, args.allow_fomod, args.source, _parse_fomod_selections(args.fomod_select), _parse_key_values(args.fomod_flag, "FOMOD flag"), args.fomod_game_version, args.dry_run, metadata_updates, args.separator, args.fomod_reuse)
            if download:
                result["download"] = download
            if args.separator:
                result["separator"] = args.separator
                if not args.dry_run:
                    ensure_separator(instance, args.profile, args.separator)
                    group_separator(instance, args.profile, args.separator, [str(result["name"])])
            _output(result, args.json)
        elif command == "mods" and args.mods_command == "remove":
            result = remove_mod(instance, args.name, args.purge, args.yes)
            _output(result, args.json)
        elif command == "mods" and args.mods_command == "rename":
            result = rename_mod(instance, args.old, args.new)
            _output(result, args.json)
        elif command == "plugins" and args.plugins_command == "list":
            data = [
                {"index": index, "name": entry.name, "enabled": entry.enabled}
                for index, entry in enumerate(ordered_plugin_entries(instance, args.profile))
            ]
            _output(data, args.json)
        elif command == "plugins" and args.plugins_command == "check":
            issues = analyze_plugins(instance, args.profile)
            _output(issues or [{"level": "ok", "message": "No plugin issues found."}], args.json)
            return 1 if any(issue["level"] == "error" for issue in issues) else 0
        elif command == "plugins" and args.plugins_command == "remove":
            _output(remove_plugin_entries(instance, args.profile, args.name), args.json)
        elif command == "plugins" and args.plugins_command == "sync":
            _output(sync_plugin_lists(instance, args.profile), args.json)
        elif command == "plugins" and args.plugins_command == "sort":
            from .plugins import sort_with_loot

            _output(sort_with_loot(instance, args.profile, args.loot_exe, args.dry_run), args.json)
        elif command == "mods" and args.mods_command == "conflicts":
            data = instance.conflicts(args.profile)
            if args.path:
                key = "/".join(Path(args.path).parts).casefold()
                data = [item for item in data if item["path"] == key]
            _output(data, args.json)
        else:
            _mutate_list(args, instance, command)
    elif command == "file-owner":
        data = instance.file_owners(args.profile, args.path)
        _output(data, args.json)
    elif command == "doctor":
        issues = instance.doctor(args.profile)
        if args.deep:
            from .plugins import analyze as deep_analyze_plugins

            issues.extend(deep_analyze_plugins(instance, args.profile))
        _output(issues or [{"level": "ok", "message": "No issues found."}], args.json)
        return 1 if any(i["level"] == "error" for i in issues) else 0
    elif command == "snapshot":
        content = instance.json_snapshot(args.profile) + "\n"
        if args.output:
            write_text(Path(args.output), content)
            print(f"Written: {args.output}")
        else:
            print(content, end="")
    elif command == "files":
        if args.files_command == "list":
            data = instance.virtual_files(args.profile)
            if args.path:
                wanted = "/".join(Path(args.path).parts).casefold()
                data = [item for item in data if item["path"] == wanted]
            if not args.all_providers:
                data = [{"path": item["path"], "winner": item["winner"]} for item in data]
            _output(data, args.json)
        elif args.files_command == "materialize":
            _output(instance.materialize(args.profile, args.destination, args.replace), args.json)
    elif command == "inis":
        if args.inis_command == "list":
            _output(list_inis(instance, args.profile), args.json)
        elif args.inis_command == "get":
            section = None if args.section == "-" else args.section
            value = get_value(instance, args.profile, args.file, section, args.key)
            _output({"file": args.file, "section": args.section, "key": args.key, "value": value}, args.json)
        elif args.inis_command == "set":
            section = None if args.section == "-" else args.section
            path = set_value(instance, args.profile, args.file, section, args.key, args.value)
            _output({"written": str(path)}, args.json)
        elif args.inis_command == "diff":
            print(diff_game_ini(instance, args.profile, args.file))
    elif command == "conflicts":
        from . import conflicts as conflict_reports

        if args.conflicts_command == "report":
            _output(conflict_reports.report(instance, args.profile, args.mod, args.ext, args.include_bsa), args.json)
        else:
            _output(conflict_reports.redundant(instance, args.profile), args.json)
    elif command == "stockgame":
        from . import stockgame

        if args.stockgame_command == "create":
            _output(stockgame.create(instance, args.source, args.to, args.expect_version, args.allow_mismatch, args.dry_run), args.json)
        else:
            result = stockgame.verify(Path(args.dir) if args.dir else instance.base / "Stock Game", args.expect_version)
            _output(result, args.json)
            return 0 if result["ok"] else 1
    elif command == "settings":
        from . import settings as ini_settings

        if args.settings_command == "list":
            _output(ini_settings.list_settings(instance, args.section, args.raw), args.json)
        elif args.settings_command == "get":
            _output(ini_settings.get_setting(instance, args.section, args.key, args.raw), args.json)
        else:
            _output(ini_settings.set_setting(instance, args.section, args.key, args.value, args.raw, args.bytearray, args.dry_run), args.json)
    elif command == "tools":
        if args.tools_command == "list":
            _output(instance.executables(), args.json)
        elif args.tools_command == "register":
            _output(register_executable(instance, args.title, args.binary, args.working_directory, args.arguments, args.replace), args.json)
        elif args.tools_command == "remove":
            _output(remove_executable(instance, args.target), args.json)
        elif args.tools_command == "run":
            _output(run_executable(instance, args.title, args.extra, args.wait), args.json)
        elif args.tools_command == "xedit-clean":
            from . import toolwrap

            result = toolwrap.xedit_clean(instance, args.profile, args.plugin, args.exe, args.log_dir, args.capture_to, args.usvfs_dir, args.moprofile, args.force, args.dry_run)
            _output(result, args.json)
            return 0 if args.dry_run or result.get("ok") else 1
        elif args.tools_command == "synthesis-run":
            from . import toolwrap

            result = toolwrap.synthesis_run(instance, args.profile, args.settings, args.output_mod, args.exe, args.profile_id, args.data_folder, args.load_order, args.extra_data, args.target_runtime, args.capture_to, args.usvfs_dir, args.dry_run)
            _output(result, args.json)
            return 0 if args.dry_run or result.get("ok") else 1
        elif args.tools_command == "pandora-run":
            from . import toolwrap

            result = toolwrap.pandora_run(instance, args.profile, args.output_mod, args.exe, args.tesv, not args.no_auto_run, not args.no_auto_close, args.capture_to, args.usvfs_dir, args.dry_run)
            _output(result, args.json)
            return 0 if args.dry_run or result.get("ok") else 1
        elif args.tools_command == "loot-sort":
            from . import toolwrap

            _output(toolwrap.loot_sort(instance, args.profile, args.exe, args.game, args.apply, args.update_masterlist, args.dry_run), args.json)
        elif args.tools_command == "bsarch":
            from . import toolwrap

            _output(toolwrap.bsarch(instance, args.profile, args.action, args.first, args.second, args.exe, args.fmt, not args.no_compress, not args.no_mt, args.share, args.filter, args.capture_to, args.dry_run), args.json)
        elif args.automation == "pandora":
            _output(run_pandora(instance, args.profile, args.output_mod, args.separator, args.tesv, not args.no_auto_run, not args.no_auto_close, args.usvfs_dir, args.destination, args.dry_run), args.json)
        elif args.automation == "bodyslide":
            _output(run_bodyslide(instance, args.profile, args.preset, args.group, args.output_mod, args.separator, args.trimorphs, args.usvfs_dir, args.destination, args.dry_run), args.json)
    elif command == "downloads":
        if args.downloads_command == "list":
            _output(list_downloads(instance, args.hash), args.json)
        elif args.downloads_command == "sync":
            from .downloads import sync_downloads

            _output(sync_downloads(instance), args.json)
        elif args.downloads_command == "audit":
            report = audit_downloads(instance)
            _output(report, args.json)
            return 0 if report["ok"] else 1
        elif args.downloads_command == "meta":
            _output(set_meta(instance, args.archive, args.direct_url, args.header, args.manual_url, args.prompt, args.nexus, args.game_name, args.meta_installed, args.dry_run), args.json)
        elif args.downloads_command == "mark-installed":
            from .downloads import mark_installed

            _output(mark_installed(instance, args.archive, installed=not args.uninstalled), args.json)
        else:
            if is_github_ref(args.url):
                _output(fetch_github_asset(instance, args.url, args.asset, args.output, args.replace, args.sha256, headers=_parse_headers(args.header)), args.json)
            elif args.url.casefold().startswith("nxm://") or "nexusmods.com/" in args.url.casefold():
                _output(download_reference(instance, args.url, args.nexus_api_key, args.game, args.file_name, args.output, args.replace, args.sha256, auto_download=getattr(args, "auto_download", False)), args.json)
            else:
                _output(fetch_download(instance, args.url, args.output, args.replace, headers=_parse_headers(args.header), expected_sha256=args.sha256), args.json)
    elif command == "mo2plugin":
        if args.mo2plugin_command == "install":
            _output(mo2plugins.install(instance, args.archive, args.name, args.source, args.replace, args.dry_run, args.direct_url), args.json)
        elif args.mo2plugin_command == "list":
            _output(mo2plugins.list_installed(instance), args.json)
        else:
            _output(mo2plugins.remove(instance, args.name, args.dry_run), args.json)
    elif command == "manifest":
        _output(apply_manifest(instance, args.path, args.profile, args.nexus_api_key, args.dry_run, args.replace, args.continue_on_error, auto_download=getattr(args, "auto_download", False)), args.json)
    return 0


def _mutates_instance(args: argparse.Namespace) -> bool:
    command = args.command
    if command in {"undo"}:
        return True
    if command == "profiles":
        return args.profiles_command != "list" and args.profiles_command != "export"
    if command == "mods":
        if args.mods_command == "install":
            return not args.dry_run
        if args.mods_command == "metadata":
            return bool(args.updates or args.sync)
        return args.mods_command in {"remove", "rename", "enable", "disable", "move", "separator"} and not (
            args.mods_command == "separator" and args.separator_command == "list"
        )
    if command == "plugins":
        return args.plugins_command in {"remove", "enable", "disable", "move", "sync"} or (
            args.plugins_command == "sort" and not args.dry_run
        )
    if command == "inis":
        return args.inis_command == "set"
    if command == "settings":
        return args.settings_command == "set" and not args.dry_run
    if command == "stockgame":
        return args.stockgame_command == "create" and not args.dry_run
    if command == "files":
        return args.files_command == "materialize"
    if command == "tools":
        if args.tools_command == "loot-sort":
            return args.apply and not args.dry_run
        if args.tools_command == "bsarch":
            return bool(args.capture_to) and not args.dry_run
        return args.tools_command in {"register", "remove"} or (args.tools_command in {"automate", "xedit-clean", "synthesis-run", "pandora-run"} and not args.dry_run)
    if command == "downloads":
        if args.downloads_command == "meta":
            return not args.dry_run
        return args.downloads_command in {"sync", "mark-installed", "fetch"}
    if command == "mo2plugin":
        return args.mo2plugin_command in {"install", "remove"} and not args.dry_run
    if command == "manifest":
        return not args.dry_run
    if command == "vfs":
        return args.vfs_command == "cleanup"
    return False


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(errors="replace")
        except Exception:
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    args.json = getattr(args, "json", False)
    if getattr(args, "non_interactive", False):
        os.environ["MO2CLI_NONINTERACTIVE"] = "1"
    try:
        if _mutates_instance(args):
            instance = Instance.open(getattr(args, "instance", None))
            with mutation_guard(instance):
                return run(args)
        return run(args)
    except (Mo2Error, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
