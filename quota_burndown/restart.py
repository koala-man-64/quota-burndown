"""Replace a Windows service without trusting a saved PID alone."""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
from pathlib import Path


def _creation_time(kernel, handle) -> int:
    from ctypes import wintypes
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    values = [wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime


def current_process_started() -> int | None:
    if os.name != "nt":
        return None
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    return _creation_time(kernel, kernel.GetCurrentProcess())


def _matches_service(argv: list[str], home: Path) -> bool:
    # Require this checkout's absolute entry point. A bare script name cannot
    # establish which checkout an older process is running from.
    script = Path(__file__).resolve().parent.parent / "quota-burndown.py"
    if not argv or Path(argv[0]).name.lower() not in {"python.exe", "pythonw.exe"}:
        return False
    args = argv[1:]
    while args and args[0] in {"-B", "-u"}:
        args = args[1:]
    if not args or not Path(args[0]).is_absolute() or Path(args[0]).resolve() != script:
        return False
    args = args[1:]
    # Do not infer another process's QUOTA_BURNDOWN_HOME environment variable.
    target_home = Path.home() / ".quota-burndown"
    if args[:1] == ["--home"] and len(args) >= 2:
        target_home = Path(args[1])
        args = args[2:]
        if not target_home.is_absolute():
            return False
    elif args and args[0].startswith("--home="):
        target_home = Path(args[0].split("=", 1)[1])
        args = args[1:]
        if not target_home.is_absolute():
            return False
    return args[:1] == ["serve"] and target_home.resolve() == home.resolve()


def stop_existing(home: Path) -> None:
    """Terminate only the recorded, verified service, retaining its OS handle."""
    if os.name != "nt":
        raise RuntimeError("automatic service replacement is currently supported on Windows only")
    try:
        metadata = json.loads((home / "capacity-service.json").read_text(encoding="utf-8"))
        pid = metadata["pid"]
        if type(pid) is not int or pid <= 0 or pid == os.getpid():
            raise ValueError("invalid service PID")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("cannot identify the existing quota service; stop it manually") from exc

    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    # Hold a handle before inspecting the command line. Even if the process
    # exits and its PID is reused, termination can only target this process.
    handle = kernel.OpenProcess(0x00100000 | 0x1000 | 0x0001, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # PID no longer exists; caller will retry the writer lock.
            return
        raise ctypes.WinError(error)
    try:
        started = metadata.get("started")
        if started is not None:
            if type(started) is not int or started != _creation_time(kernel, handle):
                raise RuntimeError(f"refusing to stop PID {pid}: saved process identity is stale")
            _terminate(kernel, handle, pid)
            return
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}').CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, check=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if kernel.WaitForSingleObject(handle, 0) == 0:
            return
        command = json.loads(result.stdout)
        if not isinstance(command, str):
            raise RuntimeError("cannot read the existing quota service command line")
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
        kernel.LocalFree.argtypes = [wintypes.HLOCAL]
        argc = ctypes.c_int()
        argv = shell.CommandLineToArgvW(command, ctypes.byref(argc))
        if not argv:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            matches = _matches_service(list(argv[:argc.value]), home)
        finally:
            kernel.LocalFree(argv)
        if not matches:
            raise RuntimeError(f"refusing to stop PID {pid}: not this checkout's quota service for {home}")
        _terminate(kernel, handle, pid)
    finally:
        kernel.CloseHandle(handle)


def _terminate(kernel, handle, pid):
    print(f"stopping existing quota service (PID {pid})", flush=True)
    if not kernel.TerminateProcess(handle, 0):
        if kernel.WaitForSingleObject(handle, 0) != 0:
            raise ctypes.WinError(ctypes.get_last_error())
    if kernel.WaitForSingleObject(handle, 10000) != 0:
        raise RuntimeError(f"quota service PID {pid} did not exit within 10 seconds")
