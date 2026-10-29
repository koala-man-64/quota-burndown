"""Keep an explicitly supplied refresh child inside its parent's lifetime."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from contextlib import contextmanager


def gated_script_command(script, *args: str) -> list[str]:
    """Wait for the parent's go byte before executing any collector code.

    If the parent dies before assigning the Windows job, its pipe closes and
    this bootstrap exits on EOF without running the collector.
    """
    bootstrap = """import sys
if sys.stdin.buffer.read(1) != b'G':
    raise SystemExit(1)
import os, runpy
sys.argv = sys.argv[1:]
sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[0])))
runpy.run_path(sys.argv[0], run_name='__main__')
"""
    return [sys.executable, "-B", "-c", bootstrap, os.fspath(script), *args]


def _create_job(child: subprocess.Popen):
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                               ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # No name or inheritable security attributes: only this parent owns the job
    # handle. Its termination closes that handle even without Python cleanup.
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        # Popen retains the actual OS process handle, avoiding PID reuse races.
        if not kernel.AssignProcessToJobObject(handle, int(child._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    return kernel, handle


def _kill_and_reap(child: subprocess.Popen) -> None:
    if child.poll() is None:
        child.kill()
    child.wait(timeout=10)


@contextmanager
def child_job(child: subprocess.Popen):
    """On Windows, kill the supplied child if this parent exits or scope ends.

    Enter immediately after Popen and keep this scope around communicate/wait.
    Use gated_script_command and release its stdin gate only inside this scope
    when the child must not run before job assignment.
    Failed job setup kills and reaps the child before propagating the error.
    Other platforms leave process lifetime management to the caller.
    """
    if os.name != "nt":
        yield child
        return
    try:
        kernel, handle = _create_job(child)
    except BaseException:
        _kill_and_reap(child)
        raise
    try:
        yield child
    finally:
        # Closing also handles exceptional scope exit while the child is alive.
        kernel.CloseHandle(handle)
        child.wait(timeout=10)
