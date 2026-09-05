"""Quota-only, redraw-safe Claude statusline handoff."""
from __future__ import annotations
import hashlib, json, os, time, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DIR = "quota-burndown-statusline"; _MAX_BYTES = 8192; _MAX_AGE = 86400; _MAX_SESSIONS = 64
_WINDOWS = {"five_hour": 300, "seven_day": 10080}
def _directory(home: Path) -> Path: return home / _DIR
def _str(d: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        if isinstance(d.get(key), (str, int)): return str(d[key])
    return ""
def _quota(payload: dict) -> dict | None:
    raw = payload.get("rate_limits") or payload.get("rateLimits")
    if not isinstance(raw, dict): return None
    out = {}
    for name, minutes in _WINDOWS.items():
        item = raw.get(name)
        if not isinstance(item, dict): continue
        used = item.get("used_percentage", item.get("used_percent"))
        if isinstance(used, bool) or not isinstance(used, (int, float)) or not 0 <= float(used) <= 100: continue
        row = {"used_percentage": float(used), "window_minutes": minutes}
        reset = item.get("resets_at", item.get("resetsAt"))
        if isinstance(reset, (str, int, float)) and not isinstance(reset, bool): row["resets_at"] = reset
        out[name] = row
    return out or None
def _atomic(path: Path, value: dict) -> None:
    raw = json.dumps(value, separators=(",", ":"))
    if len(raw.encode()) > _MAX_BYTES: raise ValueError("handoff too large")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(raw, encoding="utf-8"); os.replace(temporary, path)
def capture(payload_text: str, home: Path) -> bool:
    try:
        payload = json.loads(payload_text)
        if not isinstance(payload, dict) or not (quota := _quota(payload)): return False
        session = _str(payload, ("session_id", "sessionId", "conversation_id")) or "default"
        session_hash = hashlib.sha256(session.encode()).hexdigest()[:24]
        account = _str(payload, ("organization_id", "organizationId", "org_id")) or "local"
        scope = "acct-" + hashlib.sha256(account.encode()).hexdigest()[:16]
        digest = hashlib.sha256(json.dumps(quota, sort_keys=True).encode()).hexdigest()
        # The supported status-line payload has no quota observation timestamp.
        # Generic session/response timestamps do not prove a new quota reading.
        observed = datetime.now(timezone.utc).isoformat()
        provenance = "first_seen"
        directory = _directory(home)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (scope + "-" + digest + ".json")
        try: old = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError): old = {}
        if old.get("account_scope") == scope and old.get("digest") == digest: return False
        _atomic(target, {"session": session_hash, "account_scope": scope, "digest": digest, "observed_at": observed, "observation_time_provenance": provenance, "quota": quota})
        files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for index, path in enumerate(files):
            try:
                if index >= _MAX_SESSIONS or time.time() - path.stat().st_mtime > _MAX_AGE: path.unlink()
            except OSError: pass
        for path in list(directory.glob("*.tmp"))[:64]:
            try:
                if time.time() - path.stat().st_mtime > 3600:
                    path.unlink()
            except OSError:
                pass
        return True
    except (OSError, ValueError, TypeError, json.JSONDecodeError): return False
def read(home: Path) -> list[dict[str, Any]]:
    try: paths = sorted(_directory(home).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:_MAX_SESSIONS]
    except OSError: return []
    result=[]
    for path in paths:
        try:
            if path.stat().st_size > _MAX_BYTES:
                continue
            item=json.loads(path.read_text(encoding="utf-8"))
            if isinstance(item, dict) and isinstance(item.get("quota"), dict): result.append(item)
        except (OSError, ValueError): pass
    return sorted(result, key=lambda item: item.get("observed_at", ""))
def drain(home: Path) -> list[dict[str, Any]]: return read(home)
def handoff_directory(home: Path) -> Path: return _directory(home)
