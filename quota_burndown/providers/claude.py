"""Claude subscription quota, polled from the OAuth usage endpoint Claude Code itself uses
for /usage. The session value is read only to authorize that call and is never logged,
printed, or stored.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from ..config import WINDOW_MINUTES, claude_home, default_home
from ..store import Sample
from ..util import from_epoch, now_utc, parse_iso

PROVIDER = "claude"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
ENV_OAUTH = "QUOTA_BURNDOWN_CLAUDE_OAUTH"
OAUTH_FILE_NAME = "claude_oauth"
# Only a session with the profile scope can read the usage endpoint. The Claude Code CLI keeps
# one in ~/.claude/.credentials.json and refreshes it whenever it runs; the long-lived value from
# `claude setup-token` carries only the inference scope and is answered with HTTP 403.
SETUP_HINT = "samples resume once the Claude Code CLI has run and refreshed its session in {file}"
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


def oauth_file() -> Path:
    return default_home() / OAUTH_FILE_NAME


def read_access_token(path: Path | None = None, oauth_path: Path | None = None, env: dict | None = None) -> tuple[str | None, str | None, str]:
    """Return (access token, warning, origin). The token must never be printed or logged.

    Precedence: the QUOTA_BURNDOWN_CLAUDE_OAUTH environment variable, then a session value
    saved in ~/.quota-burndown/claude_oauth, then the Claude Code CLI's own credentials
    file. The first two exist for a session that carries the profile scope obtained some
    other way; the value `claude setup-token` prints does not qualify.
    """
    env = os.environ if env is None else env
    direct = (env.get(ENV_OAUTH) or "").strip()
    if direct:
        return direct, None, "env"
    oauth_path = oauth_path or oauth_file()
    try:
        saved = oauth_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        saved = ""
    except OSError as exc:
        return None, f"claude: cannot read {oauth_path} ({exc.__class__.__name__})", ""
    if saved:
        return saved, None, "file"

    path = path or claude_home() / ".credentials.json"
    hint = SETUP_HINT.format(file=path)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None, f"claude: no credentials file at {path}; sign in with the Claude Code CLI first", ""
    except (OSError, ValueError) as exc:
        return None, f"claude: cannot read credentials file ({exc.__class__.__name__})", ""
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    token = (oauth or {}).get("accessToken")
    if not token:
        return None, f"claude: credentials file has no OAuth session; {hint}", ""
    expires = from_epoch((oauth or {}).get("expiresAt"))
    if expires is not None and expires <= now_utc():
        return None, f"claude: CLI OAuth session expired; {hint}", ""
    return token, None, "credentials"


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


def collect(now: datetime | None = None, credentials_path: Path | None = None, timeout: float = 15.0, oauth_path: Path | None = None) -> tuple[list[Sample], str | None]:
    """Poll the usage endpoint. Returns (samples, warning)."""
    now = now or now_utc()
    token, warning, origin = read_access_token(credentials_path, oauth_path)
    if not token:
        return [], warning
    try:
        payload = fetch_usage(token, timeout=timeout)
    except ClaudeAuthError as exc:
        if origin == "credentials":
            return [], f"claude: usage endpoint rejected the CLI OAuth session ({exc}); {SETUP_HINT.format(file=credentials_path or claude_home() / '.credentials.json')}"
        where = "QUOTA_BURNDOWN_CLAUDE_OAUTH" if origin == "env" else str(oauth_path or oauth_file())
        return [], f"claude: usage endpoint rejected the session from {where} ({exc}); remove it so the CLI session is used (a `claude setup-token` value lacks the scope this endpoint needs)"
    except (RuntimeError, OSError, ValueError) as exc:
        return [], f"claude: usage fetch failed: {exc}"
    finally:
        del token
    return normalize_usage(payload, now), None
