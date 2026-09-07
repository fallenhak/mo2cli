from __future__ import annotations

import shutil
import urllib.parse
import urllib.request
from pathlib import Path

from .archives import sha256
from .workspace import Instance, Mo2Error


def list_downloads(instance: Instance, with_hash: bool = False) -> list[dict[str, object]]:
    if not instance.downloads_dir.exists():
        return []
    result = []
    for path in sorted((item for item in instance.downloads_dir.iterdir() if item.is_file()), key=lambda item: item.name.casefold()):
        item = {"name": path.name, "path": str(path), "bytes": path.stat().st_size}
        if with_hash:
            item["sha256"] = sha256(path)
        result.append(item)
    return result


def fetch(instance: Instance, url: str, output: str | None = None, replace: bool = False, headers: dict[str, str] | None = None, expected_sha256: str | None = None) -> dict[str, object]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise Mo2Error("Only HTTP(S) downloads are supported.")
    request_url = urllib.parse.urlunparse(
        parsed._replace(path=urllib.parse.quote(urllib.parse.unquote(parsed.path), safe="/:@!$&'()*+,;=-._~"))
    )
    name = output or Path(urllib.parse.unquote(parsed.path)).name
    if not name:
        raise Mo2Error("Could not determine filename from URL; specify --output.")
    target = instance.downloads_dir / Path(name).name
    if target.exists() and not replace:
        raise Mo2Error(f"Download already exists: {target}; use --replace to overwrite.")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    try:
        request = urllib.request.Request(request_url, headers=headers or {}, method="GET")
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as destination:
            shutil.copyfileobj(response, destination)
        digest = sha256(temporary)
        if expected_sha256 and digest.casefold() != expected_sha256.casefold():
            raise Mo2Error(f"SHA-256 verification failed: expected {expected_sha256}, got {digest}")
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {"url": url, "path": str(target), "bytes": target.stat().st_size, "sha256": sha256(target)}
