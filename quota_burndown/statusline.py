"""Claude Code statusLine entry point.

Prints one compact line from latest.json, the newest reading per window that the collector
wrote. It is display only: the JSON Claude Code pipes on stdin is ignored, nothing is sampled
and nothing is written, so the status bar never becomes a scheduled process of its own.
"""
from __future__ import annotations

from datetime import datetime

from .model import PROVIDER_ORDER, Burndown, from_latest
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
    """The status line for whatever latest.json holds. `stdin_text` is accepted so Claude Code's
    payload can be drained, and otherwise ignored."""
    del stdin_text
    return format_line(from_latest(store.latest(), now or now_utc()), color=color)
