"""Read-only local capacity clients. Persisted fallback never pretends to be live."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .util import now_utc, parse_iso, read_json


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def service_url(home: Path, override: str | None = None) -> str:
    from .service import loopback_host
    info = read_json(home / "capacity-service.json", {})
    url = override or (info.get("url") if isinstance(info, dict) else None) or "http://127.0.0.1:8787"
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("capacity URL must be a loopback HTTP origin")
    loopback_host(parsed.hostname or "")
    return url.rstrip("/")


def _open(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    return opener.open(url, timeout=20)


def read_capacity(home: Path, url: str | None = None) -> dict:
    endpoint = service_url(home, url) + "/v1/capacity"
    try:
        with _open(endpoint) as response:
            snapshot = json.loads(response.read(2_000_001))
        if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
            raise ValueError("unsupported capacity response")
        return snapshot
    except (OSError, urllib.error.URLError, ValueError):
        snapshot = read_json(home / "capacity.json", None)
        if not isinstance(snapshot, dict):
            raise OSError("capacity service unavailable; start quota-burndown serve") from None
        snapshot["service_state"] = "disconnected"
        now = now_utc()
        for pool in snapshot.get("pools", []):
            for window in pool.get("windows", []):
                deadline = parse_iso(window.get("valid_until"))
                window["freshness"] = "unknown" if window.get("used_pct") is None else "fresh" if deadline and now < deadline else "stale"
                observed = parse_iso(window.get("observed_at"))
                window["source_age_s"] = max(0, (now - observed).total_seconds()) if observed else None
                for key in ("runway_minutes", "exhaust_at", "whole_window_rate_pph", "recent_rate_pph", "conservative_rate_pph", "sustainable_rate_pph"):
                    window[key] = None
                reset = parse_iso(window.get("resets_at"))
                if reset and reset <= now:
                    window.update(allowance_state="unknown", remaining_pct=None, usable_pct=None)
            states = {w["allowance_state"] for w in pool.get("windows", [])}
            pool["allowance_state"] = next((s for s in ("exhausted", "unknown", "reserve_reached", "available") if s in states), "unknown")
            fresh = {w["freshness"] for w in pool.get("windows", [])}
            pool["freshness"] = "unknown" if not fresh or "unknown" in fresh else "stale" if "stale" in fresh else "fresh"
        return snapshot


def watch_capacity(home: Path, url: str | None = None):
    """Reconnect indefinitely; each connection starts with the complete current snapshot."""
    endpoint = service_url(home, url) + "/v1/capacity/events"
    delay = 1
    while True:
        try:
            with _open(endpoint) as response:
                delay = 1
                while True:
                    line = response.readline(2_000_001)
                    if not line:
                        break
                    if len(line) > 2_000_000:
                        raise ValueError("capacity event exceeds size limit")
                    if line.startswith(b"data: "):
                        value = json.loads(line[6:])
                        if isinstance(value, dict) and value.get("schema_version") == 1:
                            yield value
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(delay)
        delay = min(delay * 2, 15)
