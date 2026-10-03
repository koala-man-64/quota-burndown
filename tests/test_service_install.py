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
    assert len(calls) == 3
    assert "--home" in calls[1][-1] and "custom home" in calls[1][-1]
    assert "-WindowStyle Hidden" in calls[1][-1]


def test_uninstall_startup_removes_only_its_shortcut(monkeypatch, tmp_path):
    path = tmp_path / "service.lnk"; path.touch()
    unrelated = tmp_path / "unrelated.lnk"; unrelated.touch()
    monkeypatch.setattr(install, "startup_shortcut", lambda: path)
    monkeypatch.setattr(install.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr=""))
    assert "would remove" in install.uninstall_service()
    assert path.exists()
    assert "running process remains" in install.uninstall_service(apply=True)
    assert not path.exists() and unrelated.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows autostart")
def test_uninstall_service_removes_both_shortcut_and_task(monkeypatch, tmp_path):
    path = tmp_path / "service.lnk"; path.touch()
    monkeypatch.setattr(install, "startup_shortcut", lambda: path)
    calls = []
    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="SUCCESS", stderr="")
    monkeypatch.setattr(install.subprocess, "run", fake_run)
    result = install.uninstall_service(apply=True)
    assert not path.exists()
    assert "removed user startup shortcut" in result
    assert "scheduled task QuotaBurndownService removed" in result


@pytest.mark.skipif(os.name != "nt", reason="Windows autostart")
def test_task_already_registered_unlinks_shortcut_and_starts(monkeypatch, tmp_path):
    shortcut = tmp_path / "service.lnk"
    shortcut.touch()
    monkeypatch.setattr(install, "startup_shortcut", lambda: shortcut)
    monkeypatch.setattr(
        install.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="task_already_registered\n", stderr="")
    )
    result = install.install_service(apply=True)
    assert "already registered in Task Scheduler and started" in result
    assert not shortcut.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows autostart")
def test_task_exists_with_drift_refuses_startup_shortcut(monkeypatch, tmp_path):
    shortcut = tmp_path / "service.lnk"
    monkeypatch.setattr(install, "startup_shortcut", lambda: shortcut)
    monkeypatch.setattr(
        install.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="task_exists_drift:C:\\old\\pythonw.exe -B old.py serve\n", stderr="")
    )
    result = install.install_service(apply=True)
    assert "already exists with different action" in result
    assert not shortcut.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows autostart")
def test_task_exists_disabled_reported(monkeypatch, tmp_path):
    shortcut = tmp_path / "service.lnk"
    shortcut.touch()
    monkeypatch.setattr(install, "startup_shortcut", lambda: shortcut)
    monkeypatch.setattr(
        install.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="task_exists_disabled\n", stderr="")
    )
    result = install.install_service(apply=True)
    assert "is Disabled" in result
    assert shortcut.exists()
