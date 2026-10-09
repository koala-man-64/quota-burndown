import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import pytest

from quota_burndown.restart import _matches_service, stop_existing


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "quota-burndown.py"


def test_legacy_identity_requires_checkout_command_and_home(tmp_path):
    args = ["python.exe", "-B", str(SCRIPT), "--home", str(tmp_path), "serve"]
    assert _matches_service(args, tmp_path)
    assert not _matches_service(args, tmp_path / "other")
    assert not _matches_service([*args[:-1], "collect"], tmp_path)
    assert not _matches_service(["python.exe", "-c", "serve"], tmp_path)
    assert not _matches_service(["python.exe", str(tmp_path / "quota-burndown.py"), "serve"], tmp_path)
    assert not _matches_service(["python.exe", "quota-burndown.py", "serve"], tmp_path)


def wait_metadata(home, process):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        assert process.poll() is None, "service exited before publishing metadata"
        try:
            metadata = json.loads((home / "capacity-service.json").read_text())
            if metadata["pid"] == process.pid:
                return metadata
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    pytest.fail("service did not publish metadata in 20 seconds")


@pytest.mark.skipif(os.name != "nt", reason="Windows service replacement")
@pytest.mark.parametrize("legacy", [False, True])
def test_serve_replaces_live_process_and_reuses_port(tmp_path, legacy):
    home = tmp_path / "quota data"
    home.mkdir()
    env = os.environ.copy()
    for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "QUOTA_BURNDOWN_ANTIGRAVITY_HOME"):
        env[key] = str(tmp_path / key)
    processes = []
    with (tmp_path / "service.log").open("w") as log:
        def launch(port, absolute=False):
            process = subprocess.Popen(
                [sys.executable, "-B", str(SCRIPT) if absolute else "quota-burndown.py",
                 "--home", str(home), "serve", "--port", str(port)],
                cwd=ROOT, env=env, stdout=log, stderr=log,
            )
            processes.append(process)
            return process
        try:
            first = launch(0, absolute=legacy)
            metadata = wait_metadata(home, first)
            assert isinstance(metadata["started"], int)
            if legacy:
                del metadata["started"]
                (home / "capacity-service.json").write_text(json.dumps(metadata))
            second = launch(int(metadata["url"].rsplit(":", 1)[1]))
            fresh = wait_metadata(home, second)
            first.wait(timeout=5)
            assert fresh["url"] == metadata["url"]
            with urlopen(fresh["url"] + "/v1/capacity", timeout=5) as response:
                assert response.status == 200
                assert "revision" in json.load(response)
            # A second replacement covers relative invocation and persisted identity.
            third = launch(int(fresh["url"].rsplit(":", 1)[1]))
            wait_metadata(home, third)
            second.wait(timeout=5)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=10)


@pytest.mark.skipif(os.name != "nt", reason="Windows process verification")
@pytest.mark.parametrize("started", [None, 1])
def test_stale_metadata_never_kills_unrelated_process(tmp_path, started):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        (tmp_path / "capacity-service.json").write_text(json.dumps({"pid": process.pid, "started": started}))
        with pytest.raises(RuntimeError, match="refusing to stop"):
            stop_existing(tmp_path)
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.mark.skipif(os.name != "nt", reason="Windows process verification")
@pytest.mark.parametrize("metadata", [{}, {"pid": True}, {"pid": -1}, {"pid": os.getpid()}, []])
def test_invalid_metadata_is_rejected(tmp_path, metadata):
    (tmp_path / "capacity-service.json").write_text(json.dumps(metadata))
    with pytest.raises(RuntimeError, match="cannot identify"):
        stop_existing(tmp_path)


def test_invalid_bind_does_not_stop_existing_service(paths, monkeypatch):
    from quota_burndown.service import serve
    monkeypatch.setattr("quota_burndown.restart.stop_existing", lambda *_: pytest.fail("must not stop"))
    with pytest.raises(ValueError):
        serve(paths, host="0.0.0.0")
    with pytest.raises(ValueError):
        serve(paths, port=-1)
