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

*Note: The CLI does not reproduce MO2's Qt interface. FOMOD installation is performed deterministically from command-line options or saved decisions. Always close MO2 before modifying files directly to avoid race conditions.*

## Installation

Requires Python 3.11+:

```powershell
python -m pip install -e .
mo2 --help
```

You can also run without installation:
```powershell
python -m mo2cli --help
```

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
mo2 --json archive fomod "D:\Downloads\fomod-mod.zip"
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" --json mods show "My Mod"
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" mods metadata "My Mod" --set version=1.2.3

# Sync download metadata (*.meta) to installed mods
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" --json mods metadata --sync
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" --json mods metadata --sync --all

# Separator management and mod grouping
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" mods separator create "Core" --after "Some Mod"
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" mods separator group "Core" "SKSE" "Address Library" "Some Mod"
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" mods separator list

# Nexus/NXM download (API key can be passed via $env:NEXUS_API_KEY)
mo2 --instance "D:\Games\ModOrganizer\Skyrim" downloads fetch "nxm://skyrimspecialedition/mods/123/files/456?key=...&expires=..." --output mod.zip
mo2 --instance "D:\Games\ModOrganizer\Skyrim" downloads fetch "https://www.nexusmods.com/skyrimspecialedition/mods/123/files/456" --nexus-api-key "$env:NEXUS_API_KEY"
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" mods install "nxm://skyrimspecialedition/mods/123/files/456?key=...&expires=..." --nexus-api-key "$env:NEXUS_API_KEY" --allow-fomod
mo2 --instance "D:\Games\ModOrganizer\Skyrim" --profile "Default" mods install "https://example.test/mod.zip" --name "My Mod" --separator "Gameplay"

# Apply batch manifest (download, install, and arrange separators)
mo2 --instance "D:\Games\ModOrganizer\Skyrim" manifest apply modlist.json --nexus-api-key "$env:NEXUS_API_KEY" --dry-run
mo2 --instance "D:\Games\ModOrganizer\Skyrim" manifest apply modlist.json --nexus-api-key "$env:NEXUS_API_KEY"

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
mo2 --instance "D:\MO2\Skyrim" --profile "Default" tools automate pandora
mo2 --instance "D:\MO2\Skyrim" --profile "Default" tools automate bodyslide --preset "CBBE Curvy" --group "CBBE" --group "CBBE Vanilla Outfits" --trimorphs
```

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
```

Extraction of 7z/RAR archives requires system PATH binaries (`7z`, `7zz`, or `7za`).

## License

Distributed under the terms of the [MIT License](LICENSE).
