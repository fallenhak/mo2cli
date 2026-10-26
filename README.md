# mo2cli

A standalone, scriptable command-line interface (CLI) for managing Mod Organizer 2 (MO2) instances, profiles, and mod lists on Windows.

## Scope & Capabilities

- **FOMOD Installer Engine**: `ModuleConfig.xml` parsing, option planning, non-interactive execution (`--allow-fomod`, `--fomod-select`, `--dry-run`), decision persistence per profile (`profiles/<profile>/.mo2cli/fomod-decisions.json`), and automatic option reuse (`--fomod-reuse always` / `auto` / `never`).
- **Direct MO2 File Handling**:
  - `ModOrganizer.ini`: Instance discovery, path resolution, game selection, and active profile detection.
  - Profiles: Create, list, switch, and safely delete profiles, including ZIP export/import.
  - `modlist.txt`: Mod listing, activation/deactivation, and priority reordering.
  - `plugins.txt` & `loadorder.txt`: Plugin enable/disable and load order management.
  - Header inspection & dependency checks for TES4/ESP/ESM/ESL files.
  - Safe extraction & simple mod installation from ZIP, TAR, and 7z archives.
  - `meta.ini`: Read and update metadata (version, installation file, Nexus IDs).
  - Mod renaming and removal (deletions are moved to a recoverable trash directory by default).
  - Transaction journal with full `history` inspection and safe `undo` for install/remove/rename actions.
- **Nexus & Downloads**:
  - Direct download and installation via Nexus/NXM URLs.
  - Batch installation using JSON or TOML manifest files.
  - MO2 separator creation and mod grouping.
- **Virtual File System (VFS) & Diagnostics**:
  - Active virtual file tree analysis, file ownership lookups, conflict detection, and materializing virtual data to disk.
  - Real USVFS virtualization and hooked process execution when `usvfs_x64.dll` is present.
  - LOOT CLI integration for plugin sorting and automatic load order backups.
  - Profile INI/CFG listing, inspection, modification, and diffing against base game directories.
  - MO2 custom executable listing and direct launching.
  - JSON snapshots, export tools, and system health checks (`doctor`).

*Note: The CLI exposes the contextual FOMOD pages and choices as structured output rather than reproducing MO2's Qt interface. Installation is performed deterministically from command-line options or saved decisions. Always close MO2 before modifying files directly to avoid race conditions.*

## Installation

Requires Python 3.11+:

```powershell
python -m pip install "mo2cli @ git+https://github.com/fallenhak/mo2cli.git"
mo2 --help
```

For an editable development checkout:

```powershell
git clone https://github.com/fallenhak/mo2cli.git
cd mo2cli
python -m pip install -e ".[dev]"
python -m mo2cli --help
```

Browser-assisted Nexus commands additionally require `python -m pip install -e ".[browser]"` and `playwright install chromium`.

## Safety & Recovery

- Close Mod Organizer 2 before running commands that change an instance. Mutating commands refuse to run while MO2 is detected and use one per-instance writer lock.
- Preview installs and manifests with `--dry-run`. FOMOD installation is opt-in through `--allow-fomod` or a manifest `fomod` object.
- Archive paths, links, Windows device names, file counts, expanded size, compression ratio, and available disk space are checked before extraction where the archive backend exposes that metadata.
- Replaced and removed content is retained under `.mo2cli-trash`; successful materialization replacement reports the backup directory it preserved.
- Use `mo2 --instance "D:\Games\ModOrganizer\Skyrim" history` to inspect recorded operations and `undo` to reverse the latest supported operation.
- Keep an independent backup of an important instance. The journal and trash improve recoverability but are not a substitute for backups.

## Usage

```powershell
# Show instance summary
mo2 instances list
mo2 --instance "D:\Games\ModOrganizer\Skyrim" info

# Profile & mod listing
mo2 --instance "D:\Games\ModOrganizer\Skyrim" profiles list
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" --json mods list

# Mod modifications
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" mods disable "Some Mod"
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" mods move "Some Mod" 0

# Enable plugin
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" plugins enable "SomePlugin.esp"

# Archive installation & FOMOD options
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" mods install "D:\Downloads\fomod-mod.zip" --allow-fomod --fomod-select "Main/Variant=Recommended A" --dry-run
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" mods install "D:\Downloads\fomod-mod.zip" --allow-fomod --fomod-select "Main/Variant=Recommended A"
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" mods install "D:\Downloads\fomod-mod.zip" --allow-fomod --fomod-reuse always
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" --json mods fomod decisions
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" mods install "D:\Downloads\mod.zip" --name "My Mod"
# Inspect the pages MO2 would show using the profile's game version and
# Active/Inactive/Missing file state. Stable group keys are returned in `steps`.
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" --json archive fomod "D:\Downloads\fomod-mod.zip"
# `--instance` may be omitted when MO2_INSTANCE is set or the current directory
# is inside a discoverable portable instance.
# Preview an explicit choice without installing it.
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" --json archive fomod "D:\Downloads\fomod-mod.zip" --fomod-select "Main/Variant=Recommended A"
# Replay and compare a FOMOD Plus record.
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" --json archive fomod "D:\Downloads\fomod-mod.zip" --fomod-plus-db "D:\Games\ModOrganizer\Skyrim\fomod.db" --fomod-plus-name "Example Mod"
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" --json mods show "My Mod"
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" mods metadata "My Mod" --set version=1.2.3

# Sync download metadata (*.meta) to installed mods
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" --json mods metadata --sync
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" --json mods metadata --sync --all

# Separator management and mod grouping
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" mods separator create "Core" --after "Some Mod"
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" mods separator group "Core" "SKSE" "Address Library" "Some Mod"
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" mods separator list
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" mods separator create "Core" --color "#8caae5"
# Ordered list file (one "Name" or "Name | #RRGGBB" per line, top to bottom as in MO2). Safe to repeat.
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" mods separator apply separators.txt --dry-run
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" mods separator apply separators.txt

# Nexus/NXM download (API key can be passed via $env:NEXUS_API_KEY)
mo2 --instance "D:\Games\ModOrganizer\Skyrim" downloads fetch "nxm://skyrimspecialedition/mods/123/files/456?key=...&expires=..." --output mod.zip
mo2 --instance "D:\Games\ModOrganizer\Skyrim" downloads fetch "https://www.nexusmods.com/skyrimspecialedition/mods/123/files/456" --nexus-api-key "$env:NEXUS_API_KEY"
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" mods install "nxm://skyrimspecialedition/mods/123/files/456?key=...&expires=..." --nexus-api-key "$env:NEXUS_API_KEY" --allow-fomod
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" mods install "https://example.test/mod.zip" --name "My Mod" --separator "Gameplay"

# Autonomous Nexus download for Free accounts & AI agents (fast-forwards 5s timer)
mo2 nexus login
mo2 --instance "D:\Games\ModOrganizer\Skyrim" downloads fetch "https://www.nexusmods.com/skyrimspecialedition/mods/123?tab=files&file_id=456" --auto-download
mo2 --instance "D:\Games\ModOrganizer\Skyrim" mods install "https://www.nexusmods.com/skyrimspecialedition/mods/123?tab=files&file_id=456" --auto-download --allow-fomod

# Apply batch manifest (download, install, and arrange separators via parallel tabs)
mo2 --instance "D:\Games\ModOrganizer\Skyrim" manifest apply modlist.json --auto-download
mo2 --instance "D:\Games\ModOrganizer\Skyrim" manifest apply modlist.json --nexus-api-key "$env:NEXUS_API_KEY" --dry-run

# Profile Export & Import
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" profiles export Default-profile.zip
mo2 --instance "D:\Games\ModOrganizer\Skyrim" profiles import Default-profile.zip --name "Restored"
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" profiles settings --local-inis on --local-saves on

# Inspection & VFS Analysis
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" mods conflicts
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" file-owner "textures\foo.dds"
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" files list --all-providers
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" files materialize "D:\Temp\virtual-data"
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" doctor
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" plugins check
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" plugins sort --loot-exe "D:\MO2\loot\lootcli.exe"
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" inis list
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" inis get Skyrim.ini Display bFullScreen
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" tools list
mo2 --instance "D:\Games\ModOrganizer\Skyrim" downloads list --hash
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" snapshot -o snapshot.json
mo2 --instance "D:\Games\ModOrganizer\Skyrim" history --limit 20
mo2 --instance "D:\Games\ModOrganizer\Skyrim" undo

# Initialize a data-only instance skeleton and run game/tools
mo2 instance init "D:\MO2\Skyrim" --game-name "Skyrim Special Edition" --game-path "D:\Steam\steamapps\common\Skyrim Special Edition"
mo2 --instance "D:\MO2\Skyrim" game run --binary SkyrimSE.exe
mo2 --instance "D:\MO2\Skyrim" vfs status
mo2 --instance "D:\MO2\Skyrim" --profile "Default" vfs run "D:\Tools\xEdit.exe"

# Tool Automation: Pandora & BodySlide
mo2 --instance "D:\MO2\Skyrim" tools register "Pandora Behaviour Engine+" "D:\MO2\Skyrim\mods\Pandora\Pandora Behaviour Engine+.exe"
mo2 --instance "D:\MO2\Skyrim" tools register "BodySlide" "D:\MO2\Skyrim\mods\BodySlide\CalienteTools\BodySlide\BodySlide.exe"
mo2 --instance "D:\MO2\Skyrim" --profile "Default" tools automate pandora
mo2 --instance "D:\MO2\Skyrim" --profile "Default" tools automate bodyslide --preset "CBBE Curvy" --group "CBBE" --group "CBBE Vanilla Outfits" --trimorphs
```

The workflow contract and planned xEdit/SSEEdit, xLODGen, TexGen, and DynDOLOD support are tracked in [TOOL_WORKFLOWS.md](https://github.com/fallenhak/mo2cli/blob/main/TOOL_WORKFLOWS.md).

## Wabbajack-ready setup

These commands build a portable MO2 list that Wabbajack can compile: `downloads\` holds every installed archive with a correct `.meta`, and the game lives in `Stock Game`.

```powershell
# 1. Portable MO2 from the official 7z (ModOrganizer.ini, portable.txt, profile files, downloads\*.meta written for you)
mo2 instance init "C:\Modlists\MyList" --portable --mo2-archive "C:\Temp\Mod.Organizer-2.5.2.7z" `
    --game-name "Skyrim Special Edition" --game-path "C:\Modlists\MyList\Stock Game" `
    --mo2-url "https://github.com/ModOrganizer2/modorganizer/releases/download/v2.5.2/Mod.Organizer-2.5.2.7z" --dry-run

# 2. Extract any archive safely (zip-slip and link checks, staged, nothing touched on failure)
mo2 archive extract "C:\Temp\Stock.7z" --to "C:\Modlists\MyList\Stock Game" --strip-root

# 3. Wabbajack sources: directURL for GitHub-style links, the Nexus triple for Nexus files
mo2 --instance "C:\Modlists\MyList" downloads meta "Tool.zip" --direct-url "https://github.com/o/r/releases/download/v1/Tool.zip"
mo2 --instance "C:\Modlists\MyList" downloads meta "Mod-1234-1-0.7z" --nexus 1234:5678
mo2 --instance "C:\Modlists\MyList" downloads meta "Thing.zip" --manual-url "https://example.com/get" --prompt "Press the download button"
mo2 --instance "C:\Modlists\MyList" --json downloads audit        # installed archives without a Wabbajack source

# 4. MO2 plugins (Root Builder, FOMOD Plus, ...) into <MO2>\plugins; journaled, undoable, manifest in .mo2cli\mo2plugins.json
mo2 --instance "C:\Modlists\MyList" mo2plugin install "FOMOD Plus.zip" --direct-url "https://..." --dry-run
mo2 --instance "C:\Modlists\MyList" mo2plugin install "FOMOD Plus.zip" --direct-url "https://..."
mo2 --instance "C:\Modlists\MyList" mo2plugin list
mo2 --instance "C:\Modlists\MyList" mo2plugin remove "FOMOD Plus"
mo2 --instance "C:\Modlists\MyList" undo
```

Notes:

- `fetch --header "Name: value"` records `directURLHeaders`; signed or expiring CDN URLs are never stored as `directURL`.
- `downloads sync` also marks archives of FOMOD Plus and of plugins installed with `mo2plugin` as installed.
- `--non-interactive` (or `MO2CLI_NONINTERACTIVE=1`) stops every prompt; FOMOD reuse `auto` then behaves as without a terminal.

## Modlist tooling

Commands for building and checking a list without a GUI. Every command that changes something supports `--dry-run`, is journaled where it can be (`undo`), and takes `--json`.

```powershell
# ModOrganizer.ini (not the profile INIs): values go through the QSettings codec, other bytes stay as they were
mo2 --instance "C:\Modlists\MyList" settings list
mo2 --instance "C:\Modlists\MyList" settings list General
mo2 --instance "C:\Modlists\MyList" settings get General gamePath
mo2 --instance "C:\Modlists\MyList" settings set General selected_profile Default --dry-run
mo2 --instance "C:\Modlists\MyList" settings set Settings base_directory "%BASE_DIR%" --raw      # write the literal text
mo2 --instance "C:\Modlists\MyList" settings set General gamePath "C:\Modlists\MyList\Stock Game" --bytearray

# Stock Game: copies only vanilla files (exe, DLLs, official .esm/.bsa, Creation Club, Data\Video) and only reads --from
mo2 --instance "C:\Modlists\MyList" stockgame create --from "D:\Steam\steamapps\common\Skyrim Special Edition" --expect-version 1.6.1170.0 --dry-run
mo2 --instance "C:\Modlists\MyList" stockgame create --from "D:\Downgraded\Skyrim" --expect-version 1.6.1170.0
mo2 --instance "C:\Modlists\MyList" --json stockgame verify --expect-version 1.6.1170.0           # version, size, xxHash64 (Wabbajack base64) and SHA-256

# Nexus: --all also lists archived/old files (GraphQL v2); the default listing shows category=
mo2 --instance "C:\Modlists\MyList" nexus files 12604 --all

# GitHub releases wherever a URL is accepted (downloads fetch, mods install, manifest "url"); .meta gets the asset's browser_download_url as directURL
mo2 --instance "C:\Modlists\MyList" downloads fetch github:owner/repo@latest --asset "*.7z"
mo2 --instance "C:\Modlists\MyList" mods install github:owner/repo@v1.2 --asset "Tool-*.zip" --name "Tool"

# Conflicts between enabled mods in modlist priority order (overwrite wins first)
mo2 --instance "C:\Modlists\MyList" --json conflicts report --ext .esp,.pex,.dds
mo2 --instance "C:\Modlists\MyList" --json conflicts report --mod "Some Mod" --include-bsa   # also reads BSA v104/v105 file lists of enabled plugins
mo2 --instance "C:\Modlists\MyList" --json conflicts redundant                                  # empty mods and mods fully overwritten by others

# Tool wrappers (executables come from registered tools or --exe)
mo2 --instance "C:\Modlists\MyList" tools xedit-clean "Some Patch.esp" --capture-to "Cleaned Plugins" --dry-run
mo2 --instance "C:\Modlists\MyList" tools loot-sort                  # report only; loadorder.txt untouched
mo2 --instance "C:\Modlists\MyList" tools loot-sort --apply          # writes loadorder.txt and plugins.txt, undoable
mo2 --instance "C:\Modlists\MyList" tools bsarch pack "C:\Temp\src" "C:\Modlists\MyList\mods\X\X.bsa" --filter "*.nif"
mo2 --instance "C:\Modlists\MyList" tools bsarch list "X.bsa" --exe "C:\Tools\BSArch.exe"

# Generators run in USVFS; output goes to the real mods\<output mod> folder, --capture-to also collects stray overwrite files
mo2 --instance "C:\Modlists\MyList" tools synthesis-run --settings "C:\Modlists\MyList\Tools\Synthesis\PipelineSettings.json" --profile-id Main --target-runtime net8.0 --dry-run
mo2 --instance "C:\Modlists\MyList" tools pandora-run --output-mod "Pandora Output" --capture-to "Pandora Extras"

# Wabbajack: pre-flight report and compile (needs wabbajack-cli.exe, registered or --exe)
mo2 --instance "C:\Modlists\MyList" --json wabbajack check --settings "C:\Modlists\MyList\MyList.compiler_settings" --expect-version 1.6.1170.0
mo2 --instance "C:\Modlists\MyList" wabbajack compile --output "C:\Out\MyList.wabbajack" --settings "C:\Modlists\MyList\MyList.compiler_settings"

# Public list research (read-only; Nexus needs an API key, Load Order Library does not)
mo2 --json research collection bsyabf --revision 2
mo2 --json research lol lorerim --save-to "C:\Temp\lorerim"

# Reproducible manifest of the instance, and applying it again
mo2 --instance "C:\Modlists\MyList" manifest export "C:\Modlists\MyList\manifest.json"
mo2 --instance "C:\Modlists\MyList" manifest apply "C:\Modlists\MyList\manifest.json" --dry-run
```

Notes:

- `stockgame create` never writes to `--from`; skipped files are listed in the result. `--to` defaults to `<instance>\Stock Game`; undo moves a freshly created folder to `.mo2cli-trash`.
- `--capture-to MOD` moves files that appeared (or changed) in `overwrite` during the run into that mod (created and enabled when missing); `undo` moves them back.
- `xedit-clean` runs SSEEdit inside USVFS with `-SSE -qac -autoexit -autoload -IKnowWhatImDoing -D -P -I -R`, backs the plugin up to `.mo2cli-trash` first and refuses vanilla and Creation Club plugins without `--force`. `loot-sort` always passes a separate `--pluginListOutputPath`.
- `synthesis-run` passes `run-pipeline -o -s [-p] -d -l [-e] [-t]` (defaults: `-d` game Data, `-l` the profile's plugins.txt). `pandora-run` passes `--tesv <game root> -o <mod> --auto_run --auto_close`; `--auto_run` needs one earlier GUI run that wrote `Pandora_Engine\ActiveMods.json`, otherwise a warning is returned.
- `wabbajack check` reports downloads without a usable `.meta`, files in `overwrite` (Wabbajack ignores it), enabled generated-output mods (names such as "... Output", Pandora, Synthesis, DynDOLOD) that are neither tagged `WABBAJACK_NOMATCH_INCLUDE` in their notes nor listed in the settings file's `NoMatchInclude`, and the Stock Game `SkyrimSE.exe` version. `wabbajack compile` runs the same check first and stops unless `--force`; `-o` is passed as a folder and the new file is renamed to the requested `.wabbajack` name.
- `manifest export` writes mods (Nexus ids, directURL or manualURL from `meta.ini`, `if_missing`, `source_missing` when nothing can be downloaded), separators with colors, the exact `modlist.txt` order and enabled state (`order`), MO2 plugins, `ModOrganizer.ini` sections (`Settings`, `PluginPersistance`, `Plugins` by default) and the Stock Game version. `manifest apply` accepts these sections: `if_missing` mods that are already installed and `source_missing` mods are skipped, separator colors, order and settings are applied last (journaled, undoable), MO2 plugins are installed from `downloads\` when present, and the Stock Game version is only checked.

## Manifest Format

Manifest files can be written in JSON or TOML format. For Nexus mods, exact mod URL/NXM or `game` + `mod_id` can be specified. If no file ID is specified, the main file is selected.

```json
{
  "profile": "Default",
  "separators": [
    {"name": "Core", "mods": ["SKSE", "Address Library"]}
  ],
  "mods": [
    {
      "name": "SKSE",
      "nxm": "nxm://skyrimspecialedition/mods/30379/files/468264",
      "separator": "Core"
    },
    {
      "name": "Local Patch",
      "path": "downloads/local-patch.zip",
      "separator": "Core",
      "fomod": {"select": {"Main/Variant": ["Recommended"]}}
    }
  ]
}
```

*Nexus API usage requires a personal API key which is passed per command and never stored by `mo2cli`. Please comply with the [Nexus Mods API Acceptable Use Policy](https://help.nexusmods.com/article/114-api-acceptable-use-policy).*

## MO2 Reference

This project was designed by inspecting the official [ModOrganizer2/modorganizer](https://github.com/ModOrganizer2/modorganizer) source code and wiki, specifically referencing `Profile`, `Instance`, `Settings/PathSettings`, `modlist.txt`, and `plugins.txt` behavior.

## Development & Testing

```powershell
python -m unittest discover -s tests -v
python -m compileall mo2cli
ruff check --select E9,F63,F7,F82 mo2cli tests
python -m build
twine check dist/*
```

Extraction of 7z/RAR archives requires system PATH binaries (`7z`, `7zz`, or `7za`).

## License

Distributed under the terms of the [MIT License](LICENSE).
