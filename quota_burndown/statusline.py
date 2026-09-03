"""Claude Code statusLine entry point.

Reads the JSON Claude Code pipes on stdin, records the `rate_limits` reading it carries,
and prints one compact line. Only latest.json is read, so this stays fast enough to run on
every status refresh.
"""
from __future__ import annotations

import json
from datetime import datetime

from .model import PROVIDER_ORDER, Burndown, from_latest
from .providers import claude
from .store import Store
from .util import fmt_minutes, now_utc

_COLORS = {"over": "\x1b[31m", "under": "\x1b[32m", "exhausted": "\x1b[33m", "expired": "\x1b[2m", "idle": "\x1b[2m"}
_RESET = "\x1b[0m"


def _paint(text: str, status: str, color: bool) -> str:
    code = _COLORS.get(status) if color else None
    return f"{code}{text}{_RESET}" if code else text


def segment(bd: Burndown) -> str:
    if bd.status == "idle":
        return f"{bd.window} idle"
    if bd.status == "exhausted":
        return f"{bd.window} 100% resets {fmt_minutes(bd.remaining_min)}"
    if bd.status == "expired":
        return f"{bd.window} {bd.used:.0f}% ended"
    arrow = "▲" if bd.delta > 0 else "▼" if bd.delta < 0 else "="
    return f"{bd.window} {bd.used:.0f}% p{bd.pace:.0f} {arrow}{abs(bd.delta):.0f}"


def format_line(burndowns: list[Burndown], color: bool = True) -> str:
    if not burndowns:
        return "quota: no samples yet (run quota-burndown collect)"
    by_provider: dict[str, list[Burndown]] = {}
    for bd in burndowns:
        if bd.status == "expired":  # nothing actionable in a window that already ended
            continue
        by_provider.setdefault(bd.provider, []).append(bd)
    if not by_provider:
        return "quota: no active windows"
    parts = []
    for provider in sorted(by_provider, key=lambda p: PROVIDER_ORDER.get(p, 9)):
        segs = " · ".join(_paint(segment(bd), bd.status, color) for bd in by_provider[provider])
        parts.append(f"{provider.capitalize()} {segs}")
    return " │ ".join(parts)


def run(stdin_text: str, store: Store, now: datetime | None = None, color: bool = True) -> str:
    now = now or now_utc()
    try:
        payload = json.loads(stdin_text) if stdin_text and stdin_text.strip() else {}
    except ValueError:
        payload = {}
    if isinstance(payload, dict):
        samples = claude.normalize_statusline(payload.get("rate_limits"), now)
        if samples:
            try:
                store.append(samples)
            except OSError:
                pass
    return format_line(from_latest(store.latest(), now), color=color)
