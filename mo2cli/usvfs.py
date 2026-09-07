from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

from .workspace import Instance, Mo2Error


LINKFLAG_RECURSIVE = 0x8
INFINITE = 0xFFFFFFFF


def _disconnect_vfs(dll, timeout: float = 5.0) -> bool:
    """Disconnect USVFS without allowing a native shutdown deadlock to hang the CLI."""
    finished = threading.Event()

    def disconnect() -> None:
        try:
            dll.usvfsDisconnectVFS()
        finally:
            finished.set()

    thread = threading.Thread(target=disconnect, name="mo2cli-usvfs-disconnect", daemon=True)
    thread.start()
    thread.join(timeout)
    return finished.is_set()


class STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong), ("lpReserved", ctypes.c_wchar_p), ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p), ("dwX", ctypes.c_ulong), ("dwY", ctypes.c_ulong),
        ("dwXSize", ctypes.c_ulong), ("dwYSize", ctypes.c_ulong), ("dwXCountChars", ctypes.c_ulong),
        ("dwYCountChars", ctypes.c_ulong), ("dwFillAttribute", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
        ("wShowWindow", ctypes.c_ushort), ("cbReserved2", ctypes.c_ushort), ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", ctypes.c_void_p), ("hStdOutput", ctypes.c_void_p), ("hStdError", ctypes.c_void_p),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", ctypes.c_void_p), ("hThread", ctypes.c_void_p), ("dwProcessId", ctypes.c_ulong), ("dwThreadId", ctypes.c_ulong)]


def _runtime(instance: Instance, directory: str | None = None) -> tuple[Path, Path]:
    candidates = []
    if directory:
        candidates.append(Path(directory).expanduser())
    candidates.extend([instance.root, instance.root / "bin", instance.root.parent, instance.root.parent / "bin"])
    for candidate in candidates:
        dll = candidate / "usvfs_x64.dll"
        if dll.is_file():
            return dll, candidate
    raise Mo2Error("usvfs_x64.dll not found; specify MO2 installation directory with --usvfs-dir.")


def status(instance: Instance, directory: str | None = None) -> dict[str, object]:
    try:
        dll, root = _runtime(instance, directory)
        return {"available": True, "dll": str(dll), "directory": str(root), "proxy": str(root / "usvfs_proxy_x64.exe") if (root / "usvfs_proxy_x64.exe").exists() else None}
    except Mo2Error as error:
        return {"available": False, "message": str(error)}


def cleanup(instance: Instance, yes: bool = False) -> dict[str, object]:
    if not yes:
        raise Mo2Error("VFS staging directory cleanup requires --yes.")
    staging_root = instance.base / ".mo2cli-vfs"
    if staging_root.exists():
        if staging_root.resolve().parent != instance.base.resolve():
            raise Mo2Error("VFS staging path cannot be validated.")
        shutil.rmtree(staging_root)
    return {"removed": str(staging_root)}


def run(
    instance: Instance,
    profile: str | None,
    binary: str,
    args: list[str],
    usvfs_dir: str | None = None,
    destination: str | None = None,
    wait: bool = True,
    include_prefixes: tuple[str, ...] | None = None,
    direct_mods: bool = False,
) -> dict[str, object]:
    if os.name != "nt":
        raise Mo2Error("Real USVFS execution is only supported on Windows.")
    dll_path, dll_dir = _runtime(instance, usvfs_dir)
    executable = Path(binary).expanduser()
    if not executable.is_absolute() and instance.game_path:
        executable = instance.game_path / executable
    if not executable.is_file():
        raise Mo2Error(f"VFS executable not found: {executable}")
    game_destination = Path(destination).expanduser() if destination else ((instance.game_path / "Data") if instance.game_path and (instance.game_path / "Data").is_dir() else instance.game_path)
    if game_destination is None or not game_destination.is_dir():
        raise Mo2Error("VFS destination directory not found; specify --destination.")
    staging: Path | None = None
    if not direct_mods:
        staging = instance.base / ".mo2cli-vfs" / uuid.uuid4().hex
        staging.mkdir(parents=True, exist_ok=False)
        instance.materialize(profile, staging, include_prefixes=include_prefixes)
    sources = instance.active_mod_roots(profile) if direct_mods else [staging]
    cookie = os.add_dll_directory(str(dll_dir)) if hasattr(os, "add_dll_directory") else None
    dll = None
    params = None
    process = PROCESS_INFORMATION()
    result: dict[str, object] | None = None
    try:
        dll = ctypes.WinDLL(str(dll_path))
        dll.usvfsCreateParameters.restype = ctypes.c_void_p
        dll.usvfsSetInstanceName.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        dll.usvfsCreateVFS.argtypes = [ctypes.c_void_p]
        dll.usvfsCreateVFS.restype = ctypes.c_bool
        dll.usvfsFreeParameters.argtypes = [ctypes.c_void_p]
        dll.usvfsClearVirtualMappings.restype = None
        dll.usvfsVirtualLinkDirectoryStatic.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        dll.usvfsVirtualLinkDirectoryStatic.restype = ctypes.c_bool
        dll.usvfsDisconnectVFS.restype = None
        params = dll.usvfsCreateParameters()
        shm_name = f"mo2cli_{os.getpid()}_{uuid.uuid4().hex[:8]}".encode("ascii")
        dll.usvfsSetInstanceName(params, shm_name)
        if not dll.usvfsCreateVFS(params):
            raise Mo2Error("USVFS failed to create VFS instance.")
        dll.usvfsFreeParameters(params)
        params = None
        dll.usvfsClearVirtualMappings()
        if not direct_mods and not dll.usvfsVirtualLinkDirectoryStatic(str(staging), str(game_destination), LINKFLAG_RECURSIVE):
            raise Mo2Error("USVFS virtual folder mapping failed.")
        if direct_mods:
            for source in sources:
                if source is not None and not dll.usvfsVirtualLinkDirectoryStatic(str(source), str(game_destination), LINKFLAG_RECURSIVE):
                    raise Mo2Error(f"USVFS virtual folder mapping failed: {source}")
        startup = STARTUPINFO()
        startup.cb = ctypes.sizeof(STARTUPINFO)
        command_line = subprocess.list2cmdline([str(executable), *args])
        mutable_command = ctypes.create_unicode_buffer(command_line)
        create_process = dll.usvfsCreateProcessHooked
        create_process.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.POINTER(STARTUPINFO), ctypes.POINTER(PROCESS_INFORMATION)]
        create_process.restype = ctypes.c_bool
        if not create_process(str(executable), mutable_command, None, None, False, 0, None, str(executable.parent), ctypes.byref(startup), ctypes.byref(process)):
            raise Mo2Error(f"USVFS failed to start process: {ctypes.get_last_error()}")
        result = {"binary": str(executable), "pid": int(process.dwProcessId), "staging": str(staging) if staging else None, "mapped_sources": [str(source) for source in sources if source is not None] if direct_mods else None, "destination": str(game_destination), "virtualization": "usvfs", "waited": wait}
        if wait:
            ctypes.windll.kernel32.WaitForSingleObject(process.hProcess, INFINITE)
            exit_code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(process.hProcess, ctypes.byref(exit_code))
            result["returncode"] = int(exit_code.value)
        return result
    finally:
        if process.hThread:
            ctypes.windll.kernel32.CloseHandle(process.hThread)
        if process.hProcess:
            ctypes.windll.kernel32.CloseHandle(process.hProcess)
        if dll and hasattr(dll, "usvfsDisconnectVFS"):
            if not _disconnect_vfs(dll) and result is not None:
                result["cleanup_warning"] = "USVFS native disconnect did not finish in time; CLI continued."
        if params:
            dll.usvfsFreeParameters(params)
        if cookie:
            cookie.close()
        if wait and staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
            if staging.exists() and result is not None:
                result["cleanup_warning"] = "USVFS staging directory remained locked; can be cleaned up with vfs cleanup --yes."
