"""Claude subscription quota.

Two sources feed the same sample shape:
  * the OAuth usage endpoint Claude Code itself uses for /usage (polled), and
  * the rate_limits block Claude Code pipes to a statusLine command (pushed).
The OAuth session is read from Claude Code's credentials file only to authorize the
usage call. It is never logged, printed, or stored.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from ..config import WINDOW_MINUTES, claude_home
from ..store import Sample
from ..util import from_epoch, now_utc, parse_iso

PROVIDER = "claude"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_GROUP_MINUTES = {"session": WINDOW_MINUTES["5h"], "weekly": WINDOW_MINUTES["7d"]}
_FALLBACK_FIELDS = {
    "five_hour": ("5h", WINDOW_MINUTES["5h"]),
    "seven_day": ("7d", WINDOW_MINUTES["7d"]),
    "seven_day_opus": ("7d:opus", WINDOW_MINUTES["7d"]),
    "seven_day_sonnet": ("7d:sonnet", WINDOW_MINUTES["7d"]),
}


class ClaudeAuthError(RuntimeError):
    """The usage endpoint rejected the OAuth session."""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "scoped"


def read_access_token(path: Path | None = None) -> tuple[str | None, str | None]:
    """Return (access token, warning). The token must never be printed or logged."""
    path = path or claude_home() / ".credentials.json"
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None, f"claude: no credentials file at {path}; sign in with Claude Code first"
    except (OSError, ValueError) as exc:
        return None, f"claude: cannot read credentials file ({exc.__class__.__name__})"
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    token = (oauth or {}).get("accessToken")
    if not token:
        return None, "claude: credentials file has no OAuth session"
    expires = from_epoch((oauth or {}).get("expiresAt"))
    if expires is not None and expires <= now_utc():
        return None, "claude: OAuth session expired; it refreshes the next time Claude Code runs"
    return token, None


def fetch_usage(token: str, timeout: float = 15.0) -> dict:
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
            "User-Agent": "quota-burndown/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ClaudeAuthError(f"HTTP {exc.code}") from exc
        raise RuntimeError(f"usage endpoint returned HTTP {exc.code}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("usage endpoint returned a non-object body")
    return payload


def _window_from_limit(entry: dict) -> tuple[str, int] | None:
    kind = str(entry.get("kind") or "")
    group = str(entry.get("group") or "")
    if kind == "session":
        return "5h", WINDOW_MINUTES["5h"]
    if kind == "weekly_all":
        return "7d", WINDOW_MINUTES["7d"]
    if kind == "weekly_scoped":
        scope = entry.get("scope") or {}
        model = (scope.get("model") or {}).get("display_name") or scope.get("surface") or "scoped"
        return f"7d:{_slug(str(model))}", WINDOW_MINUTES["7d"]
    minutes = _GROUP_MINUTES.get(group) or _GROUP_MINUTES.get(kind.split("_")[0])
    if minutes:
        return _slug(kind), minutes
    return None


def normalize_usage(payload: dict, now: datetime | None = None) -> list[Sample]:
    """Samples from the usage endpoint body. Prefers the `limits` list, falls back to legacy fields."""
    now = now or now_utc()
    out: list[Sample] = []
    limits = payload.get("limits")
    if isinstance(limits, list) and limits:
        for entry in limits:
            if not isinstance(entry, dict):
                continue
            window = _window_from_limit(entry)
            percent = entry.get("percent")
            if window is None or percent is None:
                continue
            out.append(Sample(now, PROVIDER, window[0], float(percent), parse_iso(entry.get("resets_at")), window[1], "api"))
        if out:
            return out
    for field, (label, minutes) in _FALLBACK_FIELDS.items():
        block = payload.get(field)
        if not isinstance(block, dict) or block.get("utilization") is None:
            continue
        out.append(Sample(now, PROVIDER, label, float(block["utilization"]), parse_iso(block.get("resets_at")), minutes, "api"))
    return out


def normalize_statusline(rate_limits, now: datetime | None = None) -> list[Sample]:
    """Samples from the `rate_limits` object Claude Code sends to a statusLine command."""
    now = now or now_utc()
    out: list[Sample] = []
    if not isinstance(rate_limits, dict):
        return out
    for field, label in (("five_hour", "5h"), ("seven_day", "7d")):
        block = rate_limits.get(field)
        if not isinstance(block, dict) or block.get("used_percentage") is None:
            continue
        out.append(Sample(now, PROVIDER, label, float(block["used_percentage"]), from_epoch(block.get("resets_at")), WINDOW_MINUTES[label], "statusline"))
    return out


def collect(now: datetime | None = None, credentials_path: Path | None = None, timeout: float = 15.0) -> tuple[list[Sample], str | None]:
    """Poll the usage endpoint. Returns (samples, warning)."""
    now = now or now_utc()
    token, warning = read_access_token(credentials_path)
    if not token:
        return [], warning
    try:
        payload = fetch_usage(token, timeout=timeout)
    except ClaudeAuthError as exc:
        return [], f"claude: usage endpoint rejected the OAuth session ({exc}); it refreshes when Claude Code next runs"
    except (RuntimeError, OSError, ValueError) as exc:
        return [], f"claude: usage fetch failed: {exc}"
    finally:
        del token
    return normalize_usage(payload, now), None
