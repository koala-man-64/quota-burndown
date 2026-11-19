"""Claude quota polled from the usage endpoint behind Claude Code's `/usage`.

The status line only runs in a terminal `claude` session and the desktop app's history file
is written irregularly, so neither covers work done in the desktop Code tab. This adapter
reads Claude Code's OAuth access token on every poll and asks the account's own usage
endpoint for the five-hour and seven-day windows.

Credential handling is deliberately narrow:
  * the token is read per poll, held only in a local, and sent only to ENDPOINT;
  * this tool never refreshes (rotating the refresh token could sign the CLI out), writes,
    logs or includes it in an error;
  * an expired token is handed back to Claude Code: `CliRefresher` runs the CLI's own
    local `/usage` command, which refreshes and stores the token under the CLI's rules.
    Desktop app sessions take their sign-in from the app and never refresh this file;
  * a CLI this tool started is never killed, and no second one starts while it runs. Refresh
    tokens are single use: once the CLI has posted one, only that CLI holds the replacement
    until it stores it. Killing it then leaves a spent token on disk, and the next CLI run
    (this tool's or the user's) is refused with invalid_grant and clears the sign-in;
  * each refresh attempt is appended to a small log in the data home with its outcome and the
    credentials' state before and after (valid, expired, signed out...), never their content;
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

from ..util import atomic_write_text, iso, now_utc, parse_iso

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
# How long a poll waits for the CLI before leaving it to finish on its own.
REFRESH_WAIT_S = 300.0
REFRESH_LOG_MAX_BYTES = 65_536
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
        try:
            expires_s = expires / 1000
        except OverflowError:
            raise Unavailable("credentials file unreadable") from None
        if expires_s <= (time.time() if now is None else now) + 30:
            raise Expired("sign-in token expired; run `claude` in a terminal to refresh it (desktop app sessions do not)")
    return token


def credential_state(path: Path) -> str:
    """valid, expired, signed_out (the file holds no access token, as after invalid_grant),
    missing or unreadable. Safe to log: the token itself is read and dropped."""
    try:
        read_token(path)
        return "valid"
    except Expired:
        return "expired"
    except Unavailable as exc:
        return {"credentials file not found": "missing", "credentials file unreadable": "unreadable"}.get(exc.reason, "signed_out")


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


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


class CliRefresher:
    """Asks Claude Code to refresh its own expired token, one CLI at a time.

    Calling it returns None when the CLI ran cleanly, else a reason safe to surface. Output is
    discarded; the poller re-reads the credentials file to learn whether the token changed.
    A CLI still running after `wait_s`, or when the service stops, is left to finish (see the
    module notes) and a watcher logs how it ended; until then further calls start nothing."""

    def __init__(self, workdir: Path, credentials: Path, log_path: Path | None = None,
                 stop: threading.Event | None = None, wait_s: float = REFRESH_WAIT_S):
        self.workdir, self.credentials, self.log_path = workdir, credentials, log_path
        self.stop, self.wait_s = stop, wait_s
        self._running: subprocess.Popen | None = None
        self._watcher: threading.Thread | None = None
        self._log_lock = threading.Lock()

    def __call__(self) -> str | None:
        if self._running is not None:
            if self._running.poll() is None:
                self._log(outcome="skipped", reason="previous claude CLI still running")
                return "previous claude CLI still running"
            self.join(5)  # its watcher records how it ended before another CLI can change the file
        self._running = None
        if self.stop is not None and self.stop.is_set():
            return "service stopping"
        before, stamp = credential_state(self.credentials), _stamp(self.credentials)
        environ = cli_environment(os.environ)
        command = cli_command(environ)
        if command is None:
            self._log(outcome="not_found", credentials_before=before)
            return "claude CLI not found"
        started_at, started = now_utc(), time.monotonic()
        try:
            self.workdir.mkdir(parents=True, exist_ok=True)
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, cwd=self.workdir, env=environ,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, ValueError) as exc:
            self._log(outcome="failed_to_start", error=exc.__class__.__name__, credentials_before=before)
            return f"claude CLI failed to start ({exc.__class__.__name__})"
        deadline = started + self.wait_s
        while process.poll() is None:
            stopping = self.stop is not None and self.stop.is_set()
            if stopping or time.monotonic() >= deadline:
                reason = "service stopping" if stopping else f"claude CLI still running after {self.wait_s:g} s; left to finish"
                self._leave(process, started_at, started, before, stamp, reason)
                return reason
            if self.stop is not None: self.stop.wait(0.25)
            else: time.sleep(0.25)
        self._log_end(process.returncode, started_at, started, before, stamp, outcome="exited")
        return None if process.returncode == 0 else f"claude CLI exited {process.returncode}"

    def join(self, timeout: float | None = None) -> None:
        """Waits until a CLI that was left running has exited and been logged."""
        if self._watcher is not None:
            self._watcher.join(timeout)

    def _leave(self, process, started_at, started, before, stamp, reason):
        self._running = process
        self._log(outcome="left_running", reason=reason, started_at=iso(started_at),
                  waited_s=round(time.monotonic() - started, 1), credentials_before=before)

        def watch():
            try:
                self._log_end(process.wait(), started_at, started, before, stamp, outcome="finished_late")
            except Exception:  # diagnostics only; never raise into the interpreter's thread hook
                pass

        self._watcher = threading.Thread(target=watch, name="claude-refresh-watch", daemon=True)
        self._watcher.start()

    def _log_end(self, code, started_at, started, before, stamp, outcome):
        self._log(outcome=outcome, exit_code=code, started_at=iso(started_at),
                  duration_s=round(time.monotonic() - started, 1), credentials_before=before,
                  credentials_after=credential_state(self.credentials),
                  credentials_rewritten=_stamp(self.credentials) != stamp)

    def _log(self, **record):
        """One JSON line per event; the file keeps its newest half once past the size cap.
        Diagnostics only: a failed write never affects the refresh."""
        if self.log_path is None:
            return
        line = json.dumps({"at": iso(now_utc()), **record}, separators=(",", ":")) + "\n"
        with self._log_lock:
            try:
                if self.log_path.stat().st_size > REFRESH_LOG_MAX_BYTES:
                    lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
                    atomic_write_text(self.log_path, "".join(lines[len(lines) // 2:]))
            except OSError:
                pass
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
            except OSError:
                pass


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
