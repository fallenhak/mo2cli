# Tool workflow roadmap

mo2cli treats an external modding tool as a reproducible workflow, not merely an executable to launch. A workflow must be inspectable with `--dry-run`, run against the selected MO2 profile through USVFS, preserve existing output on failure, and report its command line, exit status, output location, and cleanup state.

## Current support

- Generic MO2 executable discovery and direct launch
- Generic USVFS execution
- Pandora Behaviour Engine+ workflow with a dedicated output mod
- BodySlide batch workflow with a dedicated output mod
- `tools xedit-clean PLUGIN` (SSEEdit `-qac` in USVFS, log parsed for ITM/UDR counts), `tools loot-sort [--apply]` (lootcli to a temporary `--pluginListOutputPath`, JSON summary of messages, dirty plugins and missing masters) and `tools bsarch pack|unpack|list`; `--capture-to MOD` moves new files from `overwrite` into a mod (journaled, undoable)
- `tools synthesis-run` (Synthesis.CLI `run-pipeline`) and `tools pandora-run` (Pandora `--tesv -o --auto_run --auto_close`) run in USVFS with their output written straight to `mods\<output mod>`; `--capture-to MOD` collects whatever still lands in `overwrite`
- `wabbajack check` / `wabbajack compile` (pre-flight: downloads `.meta`, non-empty `overwrite`, untagged generated-output mods, Stock Game version; compile wraps `wabbajack-cli compile -i -o`)
- `research collection SLUG` (Nexus GraphQL v2 `collectionRevision`) and `research lol SLUG` (Load Order Library public API)
- `manifest export FILE` and the extra `manifest apply` sections (order, separator colors, MO2 plugins, settings, Stock Game version)
- Tool archives for Wabbajack lists: `archive extract`, `downloads meta` / `downloads audit`, and `mo2plugin install` keep the archive in `downloads\` with a `.meta` that carries a Wabbajack source (see the README section "Wabbajack-ready setup")

## Planned workflows

1. xEdit family, including Skyrim Special Edition's SSEEdit executable
2. xLODGen
3. TexGen and DynDOLOD
4. Expanded Pandora validation and run reports

## Required contract

Each workflow should provide:

- MO2 executable discovery by configured title and executable aliases;
- a structured dry-run containing the resolved executable, working directory, arguments, selected profile, runtime/game mode, virtual Data destination, and output mod;
- fail-closed validation for missing binaries, required resources, ambiguous editions, unsafe output paths, and unsupported versions;
- USVFS execution with deterministic argument construction and no shell command interpolation;
- a staging output directory, followed by atomic promotion into the output mod only after a successful exit;
- preservation of the previous output under recoverable trash or backup storage;
- captured logs and actionable non-zero-exit diagnostics;
- journal integration so profile/output registration can be undone without overwriting later user changes.

Tool-specific command-line switches must be verified against the installed tool version or its authoritative documentation before implementation. If a required operation is only available through an interactive UI and cannot be verified reliably, the workflow must stop and report that limitation instead of guessing or clicking through dialogs.

## Expected pipeline

The intended generated-output order is Pandora, xLODGen, TexGen, and DynDOLOD, with each result registered as a separate MO2 output mod. The workflow layer must not assume that every list uses every stage, and it must leave ordering decisions explicit and inspectable for Wabbajack reproducibility.
