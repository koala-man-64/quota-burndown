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
    def antigravity_state(self) -> Path:
        return self.home / "antigravity_scan_state.json"

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


HISTORY_NAME = "plan-usage-history.json"


def claude_desktop_history_candidates() -> list[Path]:
    """Where the Claude desktop app's plan-usage history may live.

    On Windows the app is an MSIX package with AppData virtualization: the file it writes to
    `%APPDATA%\\Claude` really lands under the package's LocalCache, and only processes that
    carry the package identity (the app and its children) see it at the plain path. A
    scheduled task or a terminal started elsewhere must read the LocalCache copy."""
    override = os.environ.get("QUOTA_BURNDOWN_CLAUDE_DESKTOP_HISTORY")
    if override:
        return [Path(override).expanduser()]
    if os.name == "nt":
        roaming = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        local = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        packaged = sorted((local / "Packages").glob("Claude_*/LocalCache/Roaming/Claude/" + HISTORY_NAME))
        return [roaming / "Claude" / HISTORY_NAME, *packaged]
    if os.uname().sysname == "Darwin":  # pragma: no cover - not exercised on Windows
        return [Path.home() / "Library" / "Application Support" / "Claude" / HISTORY_NAME]
    return [Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "Claude" / HISTORY_NAME]  # pragma: no cover


def claude_desktop_history() -> Path:
    """The newest existing candidate, or the first candidate when none exists yet."""
    candidates = claude_desktop_history_candidates()
    existing = []
    for path in candidates:
        try:
            existing.append((path.stat().st_mtime, path))
        except OSError:
            continue
    if existing:
        return max(existing, key=lambda item: item[0])[1]
    return candidates[0]


def antigravity_home() -> Path:
    """Antigravity keeps its conversation store under ~/.gemini/antigravity. The override
    is this tool's own variable; Google does not define one."""
    return Path(os.environ.get("QUOTA_BURNDOWN_ANTIGRAVITY_HOME") or (Path.home() / ".gemini" / "antigravity"))


DEFAULT_ANTIGRAVITY_WEEKLY_TOKENS = 50_000_000


def antigravity_weekly_token_budget() -> int:
    env = os.environ.get("QUOTA_BURNDOWN_ANTIGRAVITY_TOKEN_BUDGET")
    if env:
        try:
            val = int(env)
            if val > 0:
                return val
        except ValueError:
            pass
    return DEFAULT_ANTIGRAVITY_WEEKLY_TOKENS


def project_root() -> Path:
    """Directory that holds the launcher script; doubles as the Claude plugin root."""
    return Path(__file__).resolve().parent.parent
