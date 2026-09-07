from __future__ import annotations

import os
from pathlib import Path

from .workspace import Instance


def global_roots() -> list[Path]:
    roots: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        roots.append(Path(local_app_data) / "ModOrganizer")
    roots.append(Path.home() / "AppData" / "Local" / "ModOrganizer")
    return list(dict.fromkeys(root.resolve() for root in roots))


def list_instances() -> list[dict[str, object]]:
    result = []
    for root in global_roots():
        if not root.is_dir():
            continue
        for directory in sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name.casefold()):
            if not (directory / "ModOrganizer.ini").is_file():
                continue
            instance = Instance.open(directory)
            result.append({"name": directory.name, "path": str(directory), "game": instance.game_name, "profile": instance.selected_profile})
    return result
