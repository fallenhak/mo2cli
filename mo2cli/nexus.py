from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .downloads import fetch
from .formats import write_text
from .workspace import Instance, Mo2Error


API_ROOT = "https://api.nexusmods.com/v1"
APPLICATION_NAME = "mo2cli"

GAME_DOMAINS = {
    "skyrim": "skyrim",
    "skyrim special edition": "skyrimspecialedition",
    "skyrim se": "skyrimspecialedition",
    "fallout 4": "fallout4",
    "fallout 3": "fallout3",
    "fallout: new vegas": "newvegas",
    "fallout new vegas": "newvegas",
    "oblivion": "oblivion",
    "morrowind": "morrowind",
    "starfield": "starfield",
    "cyberpunk 2077": "cyberpunk2077",
    "the witcher 3": "witcher3",
    "baldur's gate 3": "baldursgate3",
}


def _integer(value: object, label: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise Mo2Error(f"Nexus {label} invalid: {value}") from error


@dataclass(frozen=True)
class NexusReference:
    game: str
    mod_id: int
    file_id: int | None = None
    key: str | None = None
    expires: str | None = None


def game_domain(instance: Instance, explicit: str | None = None) -> str:
    if explicit:
        normalized = explicit.strip().casefold()
        return GAME_DOMAINS.get(normalized, normalized)
    value = instance.game_name.casefold().strip()
    if value in GAME_DOMAINS:
        return GAME_DOMAINS[value]
    if value:
        return re.sub(r"[^a-z0-9]+", "", value)
    raise Mo2Error("Nexus game domain not found; specify --game.")


def parse_reference(value: str, instance: Instance, explicit_game: str | None = None) -> NexusReference:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.casefold() == "nxm":
        game = parsed.netloc or game_domain(instance, explicit_game)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 4 or parts[0].casefold() != "mods" or parts[2].casefold() != "files":
            raise Mo2Error("Invalid NXM link; expected mods/{id}/files/{id}.")
        query = urllib.parse.parse_qs(parsed.query)
        try:
            return NexusReference(game, _integer(parts[1], "mod id"), _integer(parts[3], "file id"), query.get("key", [None])[0], query.get("expires", [None])[0])
        except ValueError as error:
            raise Mo2Error("NXM mod/file IDs must be numeric.") from error
    host = parsed.hostname.casefold() if parsed.hostname else ""
    if host not in {"nexusmods.com", "www.nexusmods.com"}:
        raise Mo2Error("Nexus reference must be a nexusmods.com URL or nxm:// link.")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3 or parts[1].casefold() != "mods":
        raise Mo2Error("Nexus URL must follow /<game>/mods/<id>.")
    file_id = None
    try:
        if len(parts) >= 5 and parts[3].casefold() == "files":
            file_id = _integer(parts[4], "file id")
        else:
            query = urllib.parse.parse_qs(parsed.query)
            raw_file_id = query.get("file_id", [None])[0]
            if raw_file_id not in (None, ""):
                file_id = _integer(raw_file_id, "file id")
        return NexusReference(parts[0], _integer(parts[2], "mod id"), file_id)
    except ValueError as error:
        raise Mo2Error("Nexus mod/file IDs must be numeric.") from error


def resolve_api_key(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit.strip()
    key_file = Path.home() / ".mo2cli" / "nexus_api_key.txt"
    if key_file.is_file():
        try:
            val = key_file.read_text(encoding="utf-8-sig").strip()
            if val:
                return val.lstrip("\ufeff").strip()
        except Exception:
            pass
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
                val, _ = winreg.QueryValueEx(key, "NEXUS_API_KEY")
                if val and str(val).strip():
                    return str(val).strip().lstrip("\ufeff").strip()
        except Exception:
            pass
    raw_env = os.environ.get("NEXUS_API_KEY")
    return raw_env.lstrip("\ufeff").strip() if raw_env else None


class NexusClient:
    def __init__(self, api_key: str | None = None, application_version: str = __version__):
        self.api_key = resolve_api_key(api_key)
        self.application_version = application_version

    def _request(self, path: str, query: dict[str, str] | None = None) -> Any:
        if not self.api_key:
            raise Mo2Error("Nexus API key required. Set NEXUS_API_KEY or specify --nexus-api-key.")
        url = f"{API_ROOT}/{path.lstrip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {
            "Application-Name": APPLICATION_NAME,
            "Application-Version": self.application_version,
            "User-Agent": f"{APPLICATION_NAME}/{self.application_version}",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["apikey"] = self.api_key
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise Mo2Error(f"Nexus API HTTP {error.code}: {detail[:500]}") from error
        except (urllib.error.URLError, json.JSONDecodeError) as error:
            raise Mo2Error(f"Nexus API request failed: {error}") from error

    def files(self, game: str, mod_id: int) -> list[dict[str, Any]]:
        result = self._request(f"games/{urllib.parse.quote(game)}/mods/{mod_id}/files")
        if isinstance(result, dict):
            result = result.get("files")
        if not isinstance(result, list):
            raise Mo2Error("Nexus files API returned unexpected data.")
        return [item for item in result if isinstance(item, dict)]

    def choose_file(self, reference: NexusReference, file_name: str | None = None) -> dict[str, Any]:
        files = self.files(reference.game, reference.mod_id)
        if reference.file_id is not None:
            candidates = []
            for item in files:
                try:
                    if int(item.get("file_id", -1)) == reference.file_id:
                        candidates.append(item)
                except (TypeError, ValueError):
                    continue
        elif file_name:
            needle = file_name.casefold()
            candidates = [item for item in files if str(item.get("name", item.get("file_name", ""))).casefold() == needle]
        else:
            candidates = [item for item in files if str(item.get("category_name", item.get("category", ""))).casefold() == "main"]
            def sort_key(item: dict[str, Any]) -> tuple[bool, int]:
                try:
                    file_id = int(item.get("file_id", 0))
                except (TypeError, ValueError):
                    file_id = 0
                return (not bool(item.get("is_primary")), file_id)
            candidates.sort(key=sort_key)
        if not candidates:
            raise Mo2Error(f"Nexus file not found: mod {reference.mod_id}, file {reference.file_id or file_name or 'main'}")
        return candidates[0]

    def download_link(self, reference: NexusReference, file_id: int) -> str:
        query = {key: value for key, value in {"key": reference.key, "expires": reference.expires}.items() if value}
        result = self._request(f"games/{urllib.parse.quote(reference.game)}/mods/{reference.mod_id}/files/{file_id}/download_link", query)
        candidates = result if isinstance(result, list) else [result]
        for item in candidates:
            if isinstance(item, dict):
                link = item.get("URI") or item.get("uri") or item.get("url")
                if link:
                    return str(link)
        raise Mo2Error("Nexus download_link API returned no valid URL.")


def download_reference(
    instance: Instance,
    reference: str | NexusReference,
    api_key: str | None = None,
    game: str | None = None,
    file_name: str | None = None,
    output: str | None = None,
    replace: bool = False,
    expected_sha256: str | None = None,
    auto_download: bool = False,
    direct_url: str | None = None,
) -> dict[str, object]:
    parsed = reference if isinstance(reference, NexusReference) else parse_reference(reference, instance, game)
    if game:
        parsed = NexusReference(game_domain(instance, game), parsed.mod_id, parsed.file_id, parsed.key, parsed.expires)
    client = NexusClient(api_key)
    if parsed.file_id is not None and not parsed.key:
        try:
            file_info = client.choose_file(parsed, file_name)
        except Mo2Error:
            file_info = {"file_id": parsed.file_id, "name": file_name or f"nexus-{parsed.mod_id}-{parsed.file_id}.zip"}
    elif parsed.file_id is not None:
        file_info = {"file_id": parsed.file_id, "name": file_name or f"nexus-{parsed.mod_id}-{parsed.file_id}.zip"}
    else:
        file_info = client.choose_file(parsed, file_name)
    file_id = _integer(file_info.get("file_id"), "file id")

    link: str | None = None
    if direct_url:
        link = direct_url
    elif auto_download and not parsed.key:
        from .browser import resolve_nxm_url
        page_url = f"https://www.nexusmods.com/{parsed.game}/mods/{parsed.mod_id}?tab=files&file_id={file_id}"
        resolved = resolve_nxm_url(page_url)
        if resolved.startswith("http://") or resolved.startswith("https://"):
            link = resolved
        else:
            parsed = parse_reference(resolved, instance, parsed.game)

    if not link:
        try:
            link = client.download_link(parsed, file_id)
        except Mo2Error as err:
            if ("403" in str(err) or "premium" in str(err).casefold()) and not parsed.key:
                from .browser import resolve_nxm_url
                page_url = f"https://www.nexusmods.com/{parsed.game}/mods/{parsed.mod_id}?tab=files&file_id={file_id}"
                print(f"Direct download link requires Premium. Attempting automated browser resolution for {page_url}...")
                resolved = resolve_nxm_url(page_url)
                if resolved.startswith("http://") or resolved.startswith("https://"):
                    link = resolved
                else:
                    parsed = parse_reference(resolved, instance, parsed.game)
                    link = client.download_link(parsed, file_id)
            else:
                raise

    archive_name = file_name or file_info.get("file_name") or file_info.get("name")
    if not file_name and link and (link.startswith("http://") or link.startswith("https://")):
        url_file = Path(urllib.parse.unquote(urllib.parse.urlsplit(link).path)).name
        if url_file and "." in url_file and not url_file.casefold().endswith((".php", ".html", ".htm")):
            archive_name = url_file
    if not archive_name:
        archive_name = f"nexus-{parsed.mod_id}-{file_id}.zip"

    result = fetch(instance, link, output or str(archive_name), replace, headers={"User-Agent": f"{APPLICATION_NAME}/{client.application_version}"}, expected_sha256=expected_sha256)
    archive_path = Path(str(result["path"]))
    stable_url = f"https://www.nexusmods.com/{parsed.game}/mods/{parsed.mod_id}?tab=files&file_id={file_id}"
    sidecar = archive_path.with_name(archive_path.name + ".meta")
    sidecar_lines = [
        "[General]",
        f"gameName={parsed.game}",
        f"modID={parsed.mod_id}",
        f"fileID={file_id}",
        f"name={archive_name}",
        f"modName={file_info.get('mod_name') or file_info.get('modName') or archive_name}",
        f"version={file_info.get('version') or ''}",
        "repository=Nexus",
        f"url={stable_url}",
        "installed=false",
        "uninstalled=false",
    ]
    write_text(sidecar, "\r\n".join(sidecar_lines) + "\r\n")
    result.update({"source": "nexus", "game": parsed.game, "mod_id": parsed.mod_id, "file_id": file_id, "file_name": archive_name, "version": file_info.get("version")})
    return result
