# Tool workflow roadmap

mo2cli treats an external modding tool as a reproducible workflow, not merely an executable to launch. A workflow must be inspectable with `--dry-run`, run against the selected MO2 profile through USVFS, preserve existing output on failure, and report its command line, exit status, output location, and cleanup state.

## Current support

- Generic MO2 executable discovery and direct launch
- Generic USVFS execution
- Pandora Behaviour Engine+ workflow with a dedicated output mod
- BodySlide batch workflow with a dedicated output mod

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
