"""Wire the tool into Claude Code, Windows Task Scheduler, and Codex. Dry-run unless apply=True."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .config import TASK_NAME, claude_home, codex_home, project_root
from .util import atomic_write_text, read_json

STATUSLINE_REFRESH_S = 60


def launcher_path() -> Path:
    return project_root() / "quota-burndown.py"


def python_launcher() -> str:
    """`py` on Windows when available (matches how Claude Code hooks are usually written), else this interpreter."""
    if os.name == "nt":
        return "py"
    return sys.executable


def statusline_command() -> str:
    return f'{python_launcher()} "{launcher_path()}" statusline'


def task_command() -> str:
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    interpreter = windowless if windowless.exists() else exe
    return f'"{interpreter}" "{launcher_path()}" collect --render --quiet'


def _backup(path: Path) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{path.name}.{stamp}.quota-burndown.bak"
    target.write_bytes(path.read_bytes())
    return target


def install_statusline(settings_path: Path | None = None, apply: bool = False, force: bool = False) -> str:
    settings_path = settings_path or claude_home() / "settings.json"
    data = read_json(settings_path, None)
    if data is None and settings_path.exists():
        return f"skip: {settings_path} is not valid JSON"
    data = data if isinstance(data, dict) else {}
    desired = {"type": "command", "command": statusline_command(), "refreshInterval": STATUSLINE_REFRESH_S}
    existing = data.get("statusLine")
    if existing == desired:
        return f"statusLine already installed in {settings_path}"
    if existing and "quota-burndown" not in json.dumps(existing) and not force:
        return f"skip: a different statusLine is configured in {settings_path} ({json.dumps(existing)}); use --force to replace it"
    if not apply:
        return f"would set statusLine in {settings_path}: {json.dumps(desired)}"
    backup = _backup(settings_path) if settings_path.exists() else None
    data["statusLine"] = desired
    atomic_write_text(settings_path, json.dumps(data, indent=2) + "\n")
    return f"statusLine set in {settings_path}" + (f" (backup: {backup})" if backup else "")


def uninstall_statusline(settings_path: Path | None = None, apply: bool = False) -> str:
    settings_path = settings_path or claude_home() / "settings.json"
    data = read_json(settings_path, None)
    if not isinstance(data, dict) or "statusLine" not in data:
        return "statusLine not installed"
    if "quota-burndown" not in json.dumps(data["statusLine"]):
        return "skip: statusLine belongs to something else"
    if not apply:
        return f"would remove statusLine from {settings_path}"
    backup = _backup(settings_path)
    del data["statusLine"]
    atomic_write_text(settings_path, json.dumps(data, indent=2) + "\n")
    return f"statusLine removed from {settings_path} (backup: {backup})"


def install_task(apply: bool = False, every_min: int = 5) -> str:
    if os.name != "nt":
        return f"skip: scheduled task is Windows-only; add a cron entry such as: */{every_min} * * * * {task_command()}"
    cmd = ["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", str(every_min), "/TN", TASK_NAME, "/TR", task_command()]
    if not apply:
        return "would run: " + subprocess.list2cmdline(cmd)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return f"schtasks failed ({result.returncode}): {(result.stderr or result.stdout).strip()}"
    return f"scheduled task {TASK_NAME} runs every {every_min} min: {task_command()}"


def uninstall_task(apply: bool = False) -> str:
    if os.name != "nt":
        return "skip: scheduled task is Windows-only"
    cmd = ["schtasks", "/Delete", "/F", "/TN", TASK_NAME]
    if not apply:
        return "would run: " + subprocess.list2cmdline(cmd)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return f"schtasks failed ({result.returncode}): {(result.stderr or result.stdout).strip()}"
    return f"scheduled task {TASK_NAME} removed"


def task_status() -> str:
    if os.name != "nt":
        return "n/a (not Windows)"
    result = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"], capture_output=True, text=True)
    if result.returncode != 0:
        return "not installed"
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return "; ".join(line for line in lines if line.split(":")[0] in ("Status", "Next Run Time", "Last Run Time"))


def install_codex_skill(home: Path | None = None, apply: bool = False) -> str:
    home = home or codex_home()
    source = project_root() / "codex" / "skills" / "quota-burndown" / "SKILL.md"
    if not source.exists():
        return f"skip: {source} missing"
    text = source.read_text(encoding="utf-8").replace("__QB_LAUNCHER__", str(launcher_path())).replace("__QB_PY__", python_launcher())
    target = home / "skills" / "quota-burndown" / "SKILL.md"
    if target.exists() and target.read_text(encoding="utf-8") == text:
        return f"codex skill already installed at {target}"
    if not apply:
        return f"would write codex skill to {target}"
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, text)
    return f"codex skill written to {target}"


def plugin_instructions() -> str:
    root = project_root()
    return (
        "Claude Code plugin (adds the on-demand /quota-burndown:burndown skill; no hooks, nothing scheduled):\n"
        f"  /plugin marketplace add {root}\n"
        "  /plugin install quota-burndown@rudy-local\n"
        f"  or for one session: claude --plugin-dir \"{root}\""
    )
