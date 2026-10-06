"""Claude quota polled from the usage endpoint behind Claude Code's `/usage`.

The status line only runs in a terminal `claude` session and the desktop app's history file
is written irregularly, so neither covers work done in the desktop Code tab. This adapter
reads Claude Code's OAuth access token on every poll and asks the account's own usage
endpoint for the five-hour and seven-day windows.

Credential handling is deliberately narrow:
  * the token is read per poll, held only in a local, and sent only to ENDPOINT;
  * it is never refreshed (rotating the refresh token could sign the CLI out), written,
    logged or included in an error; an expired token waits for Claude Code to refresh it;
  * a rejected token is not retried until the credentials file changes.

The endpoint is undocumented, so any shape it does not match yields no observation.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..util import parse_iso

ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
SOURCE = "oauth-usage"
WINDOWS = {"five_hour": 300, "seven_day": 10080}
_MAX_BYTES = 262_144


class Unavailable(Exception):
    """No usable token; `reason` is safe to surface as collector health."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


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
            raise Unavailable("token expired; waiting for Claude Code to refresh it")
    return token


def fetch(token: str, timeout: float = 15.0) -> dict:
    """GET the usage document. HTTPError propagates so callers can honor 401/429."""
    request = urllib.request.Request(ENDPOINT, headers={
        "Authorization": "Bearer " + token,
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
        "User-Agent": "quota-burndown",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
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
        return max(float(error.headers.get("Retry-After")), default)
    except (TypeError, ValueError, AttributeError):
        return default
