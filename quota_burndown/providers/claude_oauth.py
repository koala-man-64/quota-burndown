"""Claude quota polled from the usage endpoint behind Claude Code's `/usage`.

The status line only runs in a terminal `claude` session and the desktop app's history file
is written irregularly, so neither covers work done in the desktop Code tab. This adapter
reads Claude Code's OAuth access token on every poll and asks the account's own usage
endpoint for the five-hour and seven-day windows.

Credential handling is deliberately narrow:
  * the token is read per poll, held only in a local, and sent only to ENDPOINT;
  * this tool never refreshes (rotating the refresh token could sign the CLI out), writes,
    logs or includes it in an error;
  * an expired token is handed back to Claude Code: `refresh_via_cli` runs the CLI's own
    local `/usage` command, which refreshes and stores the token under the CLI's rules.
    Desktop app sessions take their sign-in from the app and never refresh this file;
  * a rejected token is not retried until the credentials file changes.

The endpoint is undocumented, so any shape it does not match yields no observation.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from ..util import parse_iso

ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
SOURCE = "oauth-usage"
WINDOWS = {"five_hour": 300, "seven_day": 10080}
_MAX_BYTES = 262_144
# `/usage` is a local command, so no model is called. The model, tool and MCP flags only
# bound what a future CLI could do if it ever sent the text to a model instead. Project-only
# settings from an empty work directory keep the user's hooks and plugins from running
# unattended; the sign-in itself is not a setting.
REFRESH_ARGS = ["-p", "/usage", "--no-session-persistence", "--model", "haiku", "--tools", "", "--strict-mcp-config",
                "--setting-sources", "project"]
REFRESH_TIMEOUT_S = 300.0
_KEEP_ENV = {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_GIT_BASH_PATH"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the Authorization header to another URL; surface it as HTTPError."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class Unavailable(Exception):
    """No usable token; `reason` is safe to surface as collector health."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Expired(Unavailable):
    """The token is past its expiry; Claude Code can refresh it."""


def credentials_path(claude_home: Path) -> Path:
    return claude_home / ".credentials.json"


def read_token(path: Path, now: float | None = None) -> str:
    """The current access token, or Unavailable. Never returns or raises with token text."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise Unavailable("credentials file not found") from None
    except (OSError, ValueError):
        raise Unavailable("credentials file unreadable") from None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token:
        raise Unavailable("no Claude subscription sign-in")
    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)) and not isinstance(expires, bool):
        if expires / 1000 <= (time.time() if now is None else now) + 30:
            raise Expired("sign-in token expired; run `claude` in a terminal to refresh it (desktop app sessions do not)")
    return token


def cli_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The service's environment without Claude/Anthropic overrides. A service started from a
    desktop Code-tab shell inherits the app's host-auth variables, and an API key would select
    key auth; either way the CLI would not refresh the subscription sign-in in this file.
    CLAUDE_CONFIG_DIR stays, made absolute because the CLI runs elsewhere, so the CLI refreshes
    the same credentials this poller reads; the git-bash location stays so the CLI can start."""
    kept = {key: value for key, value in environ.items()
            if key.upper() in _KEEP_ENV or not key.upper().startswith(("CLAUDE", "ANTHROPIC"))}
    for key in kept:
        if key.upper() == "CLAUDE_CONFIG_DIR" and kept[key]:
            kept[key] = os.path.abspath(os.path.expanduser(kept[key]))
    return kept


def cli_command(environ: Mapping[str, str]) -> list[str] | None:
    """The native install first, else an absolute PATH hit: Windows lookups also search the
    current directory, which must never supply the executable."""
    native = Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
    if native.is_file():
        return [str(native), *REFRESH_ARGS]
    found = shutil.which("claude", path=environ.get("PATH"))
    return [found, *REFRESH_ARGS] if found and Path(found).is_absolute() else None


def refresh_via_cli(workdir: Path, stop: threading.Event | None = None,
                    timeout: float = REFRESH_TIMEOUT_S) -> str | None:
    """Ask Claude Code to refresh its own expired token. Returns None when the CLI ran
    cleanly, else a reason safe to surface. Output is discarded; the poller re-reads the
    credentials file to learn whether the token actually changed."""
    environ = cli_environment(os.environ)
    command = cli_command(environ)
    if command is None:
        return "claude CLI not found"
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   cwd=workdir, env=environ, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, ValueError) as exc:
        return f"claude CLI failed to start ({exc.__class__.__name__})"
    deadline = time.monotonic() + timeout
    while process.poll() is None:
        if stop is not None and stop.is_set():
            # Left to finish: killing it could interrupt the CLI while it stores a rotated token.
            return "service stopping"
        if time.monotonic() >= deadline:
            process.kill()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: pass
            return "claude CLI timed out"
        if stop is not None: stop.wait(0.25)
        else: time.sleep(0.25)
    return None if process.returncode == 0 else f"claude CLI exited {process.returncode}"


def fetch(token: str, timeout: float = 15.0) -> dict:
    """GET the usage document. HTTPError propagates so callers can honor 401/429."""
    request = urllib.request.Request(ENDPOINT, headers={
        "Authorization": "Bearer " + token,
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
        "User-Agent": "quota-burndown",
    })
    with _OPENER.open(request, timeout=timeout) as response:
        raw = response.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise ValueError("usage response too large")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("usage response is not an object")
    return data


def quota(data: dict[str, Any]) -> dict[str, dict]:
    """The usage document as status-line-shaped windows; unknown or malformed windows are omitted."""
    out: dict[str, dict] = {}
    for name in WINDOWS:
        item = data.get(name)
        if not isinstance(item, dict):
            continue
        used = item.get("utilization")
        if isinstance(used, bool) or not isinstance(used, (int, float)) or used < 0:
            continue
        row: dict[str, Any] = {"used_percentage": min(float(used), 100.0)}
        reset = parse_iso(item.get("resets_at")) if isinstance(item.get("resets_at"), str) else None
        if reset is not None:
            row["resets_at"] = reset.isoformat()
        out[name] = row
    return out


def retry_after(error: urllib.error.HTTPError, default: float) -> float:
    try:
        value = float(error.headers.get("Retry-After"))
    except (TypeError, ValueError, AttributeError):
        return default
    return max(value, default) if math.isfinite(value) else default
