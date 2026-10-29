import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from quota_burndown import process_job


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name != "nt", reason="Windows job objects")
def test_job_setup_failure_kills_and_reaps_only_supplied_child(monkeypatch):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    def fail_setup(_child):
        raise OSError("job assignment rejected")
    monkeypatch.setattr(process_job, "_create_job", fail_setup)
    try:
        with pytest.raises(OSError, match="job assignment rejected"):
            with process_job.child_job(child):
                pytest.fail("unprotected child must not run collector body")
        assert child.poll() is not None
        assert sibling.poll() is None
    finally:
        for process in (child, sibling):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


@pytest.mark.skipif(os.name != "nt", reason="Windows job objects")
def test_job_scope_closes_with_live_child():
    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]) as child:
        with process_job.child_job(child):
            assert child.poll() is None
        assert child.poll() is not None


@pytest.mark.skipif(os.name != "nt", reason="Windows job objects")
def test_job_allows_normal_child_completion():
    with subprocess.Popen([sys.executable, "-c", "pass"]) as child:
        with process_job.child_job(child):
            assert child.wait(timeout=10) == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows job objects")
@pytest.mark.parametrize("before_assignment", [False, True])
def test_killed_parent_cannot_orphan_its_refresh_child(tmp_path, before_assignment):
    from ctypes import wintypes
    marker = tmp_path / "assigned-child.txt"
    parent_code = """
import subprocess, sys, time
from pathlib import Path
from quota_burndown.process_job import child_job
with subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']) as child:
    with child_job(child):
        Path(sys.argv[1]).write_text(str(child.pid))
        time.sleep(60)
"""
    executed = tmp_path / "collector-started.txt"
    if before_assignment:
        # Pause the actual refresh path after Popen but before job assignment.
        # The collector must remain behind its pipe gate, then exit on EOF.
        collector = tmp_path / "collector.py"
        collector.write_text(f"from pathlib import Path\nPath({str(executed)!r}).touch()\n")
        parent_code = """
import sys, threading, time
from pathlib import Path
from quota_burndown import process_job, swimlane
def pause_before_assignment(child):
    Path(sys.argv[1]).write_text(str(child.pid))
    time.sleep(60)
    raise RuntimeError('test did not terminate parent')
process_job._create_job = pause_before_assignment
swimlane.SwimlaneRefresh(home=Path(sys.argv[1]).parent / 'data',
                        launcher=Path(sys.argv[1]).parent / 'collector.py').refresh(threading.Event())
"""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    parent = subprocess.Popen([sys.executable, "-c", parent_code, str(marker)], cwd=ROOT,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    child_handle = None
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and parent.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), parent.communicate(timeout=2) if parent.poll() is not None else "no assignment marker"
        # Retain the known test child's process handle before killing the parent.
        child_handle = kernel.OpenProcess(0x00100000 | 0x0001, False, int(marker.read_text()))
        assert child_handle
        assert kernel.WaitForSingleObject(child_handle, 0) == 258  # still running
        assert not executed.exists()
        parent.kill()  # Windows TerminateProcess skips every Python finally block.
        parent.wait(timeout=10)
        assert kernel.WaitForSingleObject(child_handle, 10000) == 0
        assert not executed.exists()
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.communicate(timeout=10)
        if child_handle:
            if kernel.WaitForSingleObject(child_handle, 0) == 258:
                kernel.TerminateProcess(child_handle, 1)
                kernel.WaitForSingleObject(child_handle, 10000)
            kernel.CloseHandle(child_handle)


def test_non_windows_is_noop(monkeypatch):
    monkeypatch.setattr(process_job.os, "name", "posix")
    child = object()
    monkeypatch.setattr(process_job, "_create_job", lambda *_: pytest.fail("must not create a job"))
    with process_job.child_job(child) as yielded:
        assert yielded is child
