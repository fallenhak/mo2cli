from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import subprocess
import uuid
from collections.abc import Iterator

from .workspace import Instance, Mo2Error


def _running_process_names() -> set[str]:
    if os.name != "nt":
        return set()
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Mo2Error(f"Could not verify whether Mod Organizer is running: {error}") from error
    if result.returncode:
        raise Mo2Error("Could not verify whether Mod Organizer is running; mutation aborted.")
    names = set()
    for line in result.stdout.splitlines():
        first = line.strip().split(",", 1)[0].strip().strip('"')
        if first:
            names.add(first.casefold())
    return names


def ensure_mo2_closed() -> None:
    running = _running_process_names()
    if {"modorganizer.exe", "modorganizer2.exe"} & running:
        raise Mo2Error("Mod Organizer is running. Close MO2 before changing the instance.")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        ctypes.windll.kernel32.CloseHandle(process)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextlib.contextmanager
def mutation_guard(instance: Instance) -> Iterator[None]:
    ensure_mo2_closed()
    lock_path = instance.base / ".mo2cli.lock"
    token = uuid.uuid4().hex
    payload = {
        "pid": os.getpid(),
        "token": token,
        "created": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(2):
        try:
            descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            try:
                current = json.loads(lock_path.read_text(encoding="utf-8"))
                owner_pid = int(current.get("pid", 0))
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                raise Mo2Error(
                    f"Instance lock is unreadable; verify no mo2cli process is active, then remove: {lock_path}"
                ) from error
            if attempt == 0 and not _pid_alive(owner_pid):
                try:
                    lock_path.unlink()
                except OSError as error:
                    raise Mo2Error(f"Could not remove stale instance lock: {lock_path}") from error
                continue
            raise Mo2Error(f"Another mo2cli mutation is active for this instance (PID {owner_pid}).")
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                json.dump(payload, output)
            break
    else:  # pragma: no cover - loop always exits or raises
        raise Mo2Error(f"Could not acquire instance lock: {lock_path}")

    try:
        yield
    finally:
        try:
            current = json.loads(lock_path.read_text(encoding="utf-8"))
            if current.get("token") == token:
                lock_path.unlink()
        except (OSError, json.JSONDecodeError):
            pass
