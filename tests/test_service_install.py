import os
from types import SimpleNamespace

import pytest

from quota_burndown import install


@pytest.mark.skipif(os.name != "nt", reason="Windows autostart")
def test_denied_scheduler_uses_user_startup_and_preserves_home(monkeypatch, tmp_path):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return SimpleNamespace(returncode=1, stderr="HRESULT 0x80070005 Access is denied", stdout="")
        return SimpleNamespace(returncode=0, stderr="", stdout="")
    monkeypatch.setattr(install.subprocess, "run", run)
    monkeypatch.setattr(install, "startup_shortcut", lambda: tmp_path / "service.lnk")
    result = install.install_service(apply=True, home=str(tmp_path / "custom home"))
    assert "per-user Startup" in result
    assert len(calls) == 2
    assert "--home" in calls[1][-1] and "custom home" in calls[1][-1]
    assert "-WindowStyle Hidden" in calls[1][-1]


def test_uninstall_startup_removes_only_its_shortcut(monkeypatch, tmp_path):
    path = tmp_path / "service.lnk"; path.touch()
    unrelated = tmp_path / "unrelated.lnk"; unrelated.touch()
    monkeypatch.setattr(install, "startup_shortcut", lambda: path)
    assert "would remove" in install.uninstall_service()
    assert path.exists()
    assert "running process remains" in install.uninstall_service(apply=True)
    assert not path.exists() and unrelated.exists()
