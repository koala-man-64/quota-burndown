"""Paths and constants shared by every module."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_HOME = "QUOTA_BURNDOWN_HOME"
TASK_NAME = "QuotaBurndownCollect"
DEFAULT_PORT = 8787

# Known window lengths in minutes, keyed by the short label used in samples and the UI.
WINDOW_MINUTES = {"1h": 60, "5h": 300, "1d": 1440, "7d": 10080}


def label_for_minutes(minutes: int | None) -> str:
    for label, m in WINDOW_MINUTES.items():
        if m == minutes:
            return label
    return f"{minutes}m" if minutes else "?"


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def samples(self) -> Path:
        return self.home / "samples.jsonl"

    @property
    def latest(self) -> Path:
        return self.home / "latest.json"

    @property
    def codex_state(self) -> Path:
        return self.home / "codex_scan_state.json"

    @property
    def claude_desktop_state(self) -> Path:
        return self.home / "claude_desktop_state.json"

    @property
    def html(self) -> Path:
        return self.home / "burndown.html"

    @property
    def usage_db(self) -> Path:
        return self.home / "usage.sqlite"

    @property
    def log(self) -> Path:
        return self.home / "collect.log"

    @property
    def lock(self) -> Path:
        return self.home / ".lock"


def default_home() -> Path:
    env = os.environ.get(ENV_HOME)
    return Path(env).expanduser() if env else Path.home() / ".quota-burndown"


def get_paths(home: Path | str | None = None) -> Paths:
    paths = Paths(Path(home).expanduser() if home else default_home())
    paths.home.mkdir(parents=True, exist_ok=True)
    return paths


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def claude_desktop_history() -> Path:
    """The plan-usage history the Claude desktop app writes for its own usage display."""
    override = os.environ.get("QUOTA_BURNDOWN_CLAUDE_DESKTOP_HISTORY")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    elif os.uname().sysname == "Darwin":  # pragma: no cover - not exercised on Windows
        base = Path.home() / "Library" / "Application Support"
    else:  # pragma: no cover
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "Claude" / "plan-usage-history.json"


def antigravity_home() -> Path:
    """Antigravity keeps its conversation store under ~/.gemini/antigravity. The override
    is this tool's own variable; Google does not define one."""
    return Path(os.environ.get("QUOTA_BURNDOWN_ANTIGRAVITY_HOME") or (Path.home() / ".gemini" / "antigravity"))


def project_root() -> Path:
    """Directory that holds the launcher script; doubles as the Claude plugin root."""
    return Path(__file__).resolve().parent.parent
