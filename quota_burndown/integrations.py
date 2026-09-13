"""Realtime, local-only quota adapters.

These adapters publish observations quickly and leave aggregation, persistence, and
scheduling decisions to the capacity service.
"""
from __future__ import annotations

import hashlib
import json
import platform
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .capacity import Observation
from .handoff import drain

_CLAUDE_WINDOWS = {"five_hour": 300, "fiveHour": 300, "seven_day": 10080, "sevenDay": 10080}

_UTC = timezone.utc

@dataclass
class AdapterContext:
    """Shared only inside this service process; no identity is persisted."""
    codex_scope: str | None = None
    codex_obtained_at: datetime | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def codex_identity(self) -> tuple[str | None, datetime | None]:
        with self.lock: return self.codex_scope, self.codex_obtained_at


def _now() -> datetime:
    return datetime.now(_UTC)


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), _UTC)
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(_UTC)
        except ValueError:
            return None
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 100 else None


def _window_minutes(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return None
    return minutes if minutes > 0 else None


def _scope(value: Any) -> str:
    raw = str(value or "")
    if not raw: return ""
    # account identifiers are stable only within this machine; never emit an email/account id.
    return "acct-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class ActiveRolloutTail:
    """Small, offset-based tail of only today's active Codex session files."""
    def __init__(self, home: Path, context: AdapterContext):
        self.home, self.context = home, context
        self.state: dict[Path, dict[str, Any]] = {}

    def _files(self) -> list[Path]:
        today = _now().date()
        files: list[Path] = []
        for day in (today, today - timedelta(days=1)):
            folder = self.home / "sessions" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
            try: files.extend(path for path in folder.glob("*.jsonl") if path.is_file())
            except OSError: pass
        try: return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)[:8]
        except OSError: return []

    def poll(self) -> list[Observation]:
        scope, obtained = self.context.codex_identity()
        if not scope or not obtained: return []
        out: list[Observation] = []
        files = self._files()
        # Keep memory bounded even as sessions rotate throughout a long-lived service.
        self.state = {p: row for p, row in self.state.items() if p in files}
        for path in files:
            try:
                size = path.stat().st_size
                row = self.state.get(path)
                if row is None or size < row["offset"]:
                    offset = max(0, size - 1_048_576)
                    row = {"offset": offset, "model": "", "thread": "", "partial": b"", "skip": offset > 0}
                    self.state[path] = row
                with path.open("rb") as handle:
                    handle.seek(row["offset"])
                    data = handle.read(1_048_576)
                    row["offset"] = handle.tell()
                if not data:
                    continue
                chunks = (row["partial"] + data).split(b"\n")
                row["partial"] = chunks.pop()
                for raw in chunks:
                    if row["skip"]:
                        row["skip"] = False
                        continue
                    if len(raw) > 65_536:
                        continue
                    try:
                        event = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    payload = event.get("payload")
                    if event.get("type") == "turn_context" and isinstance(payload, dict):
                        row["model"] = str(payload.get("model") or row["model"])
                        continue
                    if event.get("type") == "session_meta" and isinstance(payload, dict):
                        row["thread"] = str(payload.get("id") or row["thread"])
                        continue
                    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
                    observed = _parse_time(event.get("timestamp"))
                    if not isinstance(limits, dict) or not observed or observed < obtained:
                        continue
                    limit_id = str(limits.get("limit_id") or "")
                    if not limit_id:
                        continue
                    for block in (limits.get("primary"), limits.get("secondary")):
                        if not isinstance(block, dict):
                            continue
                        minutes = _window_minutes(block.get("window_minutes") or block.get("windowDurationMins"))
                        if not minutes:
                            continue
                        out.append(Observation("codex", scope, limit_id, f"{minutes}m", minutes,
                            _number(block.get("used_percent")), _parse_time(block.get("resets_at")),
                            observed, _now(), "rollout", "reported", (row["model"],) if row["model"] else (),
                            "observed" if row["model"] else "unknown",
                            hashlib.sha256(f"{event.get('timestamp')}:{limit_id}:{minutes}".encode()).hexdigest()[:24]))
                if len(row["partial"]) > 65_536:
                    row["partial"] = b""
                    row["skip"] = True
            except OSError:
                continue
        return out


def observations_from_limits(payload: Any, *, provider: str, account_scope: str, source: str,
                             observed_at: datetime | None = None, models: tuple[str, ...] = (),
                             mapping_confidence: str = "reported", reset_provenance: str = "reported",
                             complete_snapshot: bool = False, observation_time_provenance: str = "reported") -> list[Observation]:
    """Normalize direct provider limits; missing fields remain absent observations."""
    observed_at = observed_at or _now()
    receipt = _now()
    root = payload if isinstance(payload, dict) else {}
    if provider == "claude" and any(key in root for key in _CLAUDE_WINDOWS):
        limits = {"claude": {"windows": [dict(value, window_minutes=minutes, window=name) for name, minutes in _CLAUDE_WINDOWS.items() if isinstance((value := root.get(name)), dict)]}}
    else:
        limits = root.get("rateLimitsByLimitId") or root.get("rate_limits_by_limit_id") or root.get("rate_limits") or root.get("rateLimits") or root
        if isinstance(limits, dict) and ("primary" in limits or "secondary" in limits):
            limits = {str(limits.get("limit_id") or limits.get("limitId") or "codex"): limits}
    raw_credits = root.get("rateLimitResetCredits") or root.get("rate_limit_reset_credits") or {}
    all_credits: list[dict] = []
    if isinstance(raw_credits, dict):
        for c in raw_credits.get("credits") or []:
            if isinstance(c, dict) and c.get("status") in ("available", None):
                all_credits.append({
                    "id": str(c.get("id") or ""),
                    "reset_type": str(c.get("resetType") or c.get("reset_type") or "codexRateLimits"),
                    "status": str(c.get("status") or "available"),
                    "granted_at": _parse_time(c.get("grantedAt") or c.get("granted_at")),
                    "expires_at": _parse_time(c.get("expiresAt") or c.get("expires_at")),
                    "title": str(c.get("title") or "Reset credit"),
                })
    iterable = limits.items() if isinstance(limits, dict) else enumerate(limits) if isinstance(limits, list) else []
    out: list[Observation] = []
    for fallback_id, item in iterable:
        if not isinstance(item, dict):
            continue
        limit_id = str(item.get("limitId") or item.get("limit_id") or item.get("id") or fallback_id)
        item_models = item.get("models") or item.get("model") or models
        if isinstance(item_models, str):
            item_models = (item_models,)
        elif isinstance(item_models, list):
            item_models = tuple(str(model) for model in item_models if model)
        elif not isinstance(item_models, tuple):
            item_models = models
        windows = item.get("windows") or item.get("limits") or item.get("rateLimits")
        if str(fallback_id) in _CLAUDE_WINDOWS:
            windows = [dict(item, window_minutes=_CLAUDE_WINDOWS[str(fallback_id)])]
        if windows is None and (isinstance(item.get("primary"), dict) or isinstance(item.get("secondary"), dict)):
            windows = [item.get("primary"), item.get("secondary")]
        if windows is None:
            windows = [item]
        if isinstance(windows, dict):
            windows = windows.values()
        matching_credits = tuple(c for c in all_credits if c.get("reset_type") in ("codexRateLimits", limit_id) or limit_id == "codex")
        for block in windows if isinstance(windows, (list, tuple)) or hasattr(windows, "__iter__") else []:
            if not isinstance(block, dict):
                continue
            minutes = _window_minutes(block.get("windowDurationMins") or block.get("windowMinutes") or block.get("window_minutes") or block.get("window"))
            used = _number(block.get("usedPercent") if "usedPercent" in block else block.get("used_percentage") if "used_percentage" in block else block.get("used_percent"))
            if minutes is None:
                continue
            window = f"{minutes}m"
            kwargs = dict(provider=provider, account_scope=account_scope, limit_id=limit_id,
                                   window=window, window_min=minutes, used_pct=used,
                                   resets_at=_parse_time(block.get("resetsAt") or block.get("resets_at") or block.get("resets_at_epoch")),
                                   observed_at=observed_at, received_at=receipt, source=source,
                                   reset_provenance=reset_provenance, models=tuple(item_models),
                                   mapping_confidence=mapping_confidence,
                                   observation_id=str(block.get("id") or ""), complete_snapshot=complete_snapshot,
                                   reset_credits=matching_credits)
            kwargs["observation_time_provenance"] = observation_time_provenance
            out.append(Observation(**kwargs))
    return out


def missing_windows(previous: dict[tuple[str, str, str, str], Observation], current: list[Observation]) -> list[Observation]:
    """Carry an omitted formerly-applicable window forward as explicitly unknown."""
    now = _now()
    present = {(item.provider, item.account_scope, item.limit_id, item.window) for item in current}
    unknown: list[Observation] = []
    for key, prior in list(previous.items()):
        same_pool = key[:3] in {candidate[:3] for candidate in present}
        if same_pool and key not in present and prior.used_pct is not None:
            item = Observation(provider=prior.provider, account_scope=prior.account_scope, limit_id=prior.limit_id,
                               window=prior.window, window_min=prior.window_min, used_pct=None, resets_at=None,
                               observed_at=now, received_at=now, source=prior.source,
                               reset_provenance="unknown", models=prior.models,
                               mapping_confidence=prior.mapping_confidence)
            unknown.append(item)
            previous[key] = item
    for item in current:
        previous[(item.provider, item.account_scope, item.limit_id, item.window)] = item
    return unknown


def _command() -> list[str] | None:
    # A .cmd shim cannot be used as a hidden long-lived Windows subprocess.
    executable = shutil.which("codex.exe")
    if executable:
        return [executable, "app-server"]
    launcher = shutil.which("codex") or shutil.which("codex.ps1")
    if not launcher:
        return None
    if Path(launcher).suffix.lower() not in {".cmd", ".ps1", ".bat"}:
        return [launcher, "app-server"]
    architecture = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine().lower())
    if not architecture:
        return None
    target = "aarch64" if architecture == "arm64" else "x86_64"
    packages = Path(launcher).parent / "node_modules" / "@openai"
    package = packages / "codex"
    # npm can nest or hoist the optional platform package. Older CLI releases
    # bundle the vendor directory directly in the main package.
    for root in (package / "node_modules" / "@openai" / f"codex-win32-{architecture}",
                 packages / f"codex-win32-{architecture}", package):
        binary = root / "vendor" / f"{target}-pc-windows-msvc" / "bin" / "codex.exe"
        if binary.is_file():
            return [str(binary), "app-server"]
    return None


def _run_native(stop: threading.Event, publish: Callable[[list[Observation]], None],
                health: Callable[[str, str, str | None], None], active: Callable[[], bool], context: AdapterContext | None = None) -> None:
    command = _command()
    if command is None:
        health("codex", "unavailable", "codex launcher not found")
        return
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, encoding="utf-8", bufsize=1, creationflags=flags)
    messages: queue.Queue[str | None] = queue.Queue(maxsize=256)
    def reader() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            try: messages.put(line, timeout=0.1)
            except queue.Full: pass
        messages.put(None)
    threading.Thread(target=reader, daemon=True).start()
    sequence = 0
    scope = _scope("local")
    previous: dict[tuple[str, str, str, str], Observation] = {}
    def send(method: str, params: dict[str, Any] | None = None, notification: bool = False) -> int | None:
        nonlocal sequence
        assert process.stdin is not None
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notification:
            sequence += 1
            message["id"] = sequence
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        return None if notification else sequence
    try:
        initialize_id = send("initialize", {"clientInfo": {"name": "quota-burndown", "version": "0"}})
        initialize_deadline = time.monotonic() + 10
        initialized = False
        account_request: int | None = None
        account_deadline = 0.0
        account_ready = False
        quota_request: int | None = None
        quota_deadline = 0.0
        next_poll = 0.0
        last_poll = 0.0
        health("codex", "starting", None)
        while not stop.is_set() and process.poll() is None:
            now = time.monotonic()
            if not initialized and now >= initialize_deadline: raise OSError("initialize deadline")
            if initialized and not account_ready and now >= account_deadline: raise OSError("account deadline")
            if active():
                next_poll = min(next_poll, last_poll + 15)
            if quota_request is not None and now >= quota_deadline:
                raise OSError("quota response deadline")
            if initialized and account_ready and now >= next_poll and quota_request is None:
                quota_request = send("account/rateLimits/read")
                last_poll = now
                quota_deadline = now + 10
                next_poll = now + (15 if active() else 60)
            try: line = messages.get(timeout=0.25)
            except queue.Empty: continue
            if line is None: break
            try: message = json.loads(line)
            except ValueError: continue
            if not isinstance(message, dict):
                continue
            method = message.get("method") if isinstance(message, dict) else None
            result = message.get("result") if isinstance(message, dict) else None
            params = message.get("params") if isinstance(message, dict) else None
            if message.get("id") == initialize_id and "result" in message and not initialized:
                send("initialized", notification=True)
                account_request = send("account/read")
                account_deadline = time.monotonic() + 10
                initialized = True
                continue
            if method == "account/updated":
                account_ready = False
                quota_request = None
                if context:
                    with context.lock:
                        context.codex_scope = None
                        context.codex_obtained_at = None
                account_request = send("account/read")
                account_deadline = time.monotonic() + 10
                continue
            if isinstance(message, dict) and message.get("error"):
                raise OSError("app-server response error")
            payload = params if method == "account/rateLimits/updated" else result
            quota_response = quota_request is not None and message.get("id") == quota_request
            if quota_response:
                quota_request = None
            if message.get("id") == account_request and isinstance(payload, dict):
                account = payload.get("account") if isinstance(payload.get("account"), dict) else payload
                scope = _scope(account.get("id") or account.get("accountId") or account.get("email"))
                if not scope:
                    health("codex", "unavailable", "account identity unavailable")
                    continue
                if context:
                    with context.lock:
                        if context.codex_scope != scope:
                            context.codex_scope = scope
                            context.codex_obtained_at = _now()
                health("codex", "healthy", None)
                account_ready = True
                next_poll = 0.0
            if account_ready and isinstance(payload, dict) and (method == "account/rateLimits/updated" or quota_response):
                readings = observations_from_limits(payload, provider="codex", account_scope=scope, source="app-server",
                    complete_snapshot=quota_response, observation_time_provenance="read_completed")
                if not readings and quota_response:
                    now_received = _now()
                    readings = [Observation("codex", scope, "unreported", "unknown", 1, None, None,
                                            now_received, now_received, "app-server", "unknown", complete_snapshot=True)]
                if readings:
                    publish(readings)
                    credits_found = [c for r in readings for c in r.reset_credits]
                    if credits_found:
                        try:
                            from .config import default_home
                            from .util import atomic_write_text, iso
                            ser = []
                            for c in credits_found:
                                cd = dict(c)
                                for k in ("granted_at", "expires_at"):
                                    if isinstance(cd.get(k), datetime):
                                        cd[k] = iso(cd[k])
                                ser.append(cd)
                            atomic_write_text(default_home() / "reset_credits.json", json.dumps({"codex": ser}, separators=(",", ":")))
                        except Exception:
                            pass
    finally:
        process.terminate()
        try: process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def run_codex(stop: threading.Event, publish: Callable[[list[Observation]], None], health: Callable[[str, str, str | None], None], active: Callable[[], bool], context: AdapterContext | None = None) -> None:
    """Reconnect a native app-server with bounded exponential backoff."""
    delay = 1.0
    while not stop.is_set():
        try:
            _run_native(stop, publish, health, active, context)
            delay = 1.0
        except (OSError, ValueError, BrokenPipeError) as exc:
            health("codex", "degraded", exc.__class__.__name__)
        if stop.wait(delay): return
        delay = min(delay * 2, 60.0)


def run_files(stop: threading.Event, publish: Callable[[list[Observation]], None], health: Callable[[str, str, str | None], None], paths: Any, context: AdapterContext | None = None) -> None:
    """Drain Claude statusline handoffs.  A failed provider never stops other collectors."""
    home = Path(paths) if isinstance(paths, (str, Path)) else Path(paths.home)
    seen: dict[str, str] = {}
    next_desktop = 0.0
    next_rollout = 0.0
    from .config import codex_home
    tail = ActiveRolloutTail(codex_home(), context) if context else None
    while not stop.is_set():
        try:
            records = drain(home)
            for record in records:
                marker = str(record.get("digest") or "")
                session = str(record.get("account_scope") or "") + ":" + str(record.get("session") or "")
                if marker and seen.get(session) == marker:
                    continue
                seen[session] = marker
                observed = _parse_time(record.get("observed_at"))
                if observed is None: continue
                readings = observations_from_limits(record.get("quota"), provider="claude", account_scope=str(record.get("account_scope") or "local"),
                                                   source="statusline", observed_at=observed,
                                                   mapping_confidence="reported", reset_provenance="reported",
                                                   observation_time_provenance=str(record.get("observation_time_provenance") or "reported"))
                if readings: publish(readings)
            if time.monotonic() >= next_desktop:
                from .providers import claude_desktop
                state_path = home / "capacity-desktop-state.json"
                samples, _, _ = claude_desktop.collect(state_path)
                desktop = [Observation("claude", "acct-" + hashlib.sha256(b"local").hexdigest()[:16], "claude", f"{sample.window_min}m", sample.window_min,
                                       sample.used, sample.resets_at, sample.ts, _now(), "desktop-history",
                                       "inferred" if sample.resets_at else "unknown") for sample in samples]
                if desktop: publish(desktop)
                next_desktop = time.monotonic() + 60
            health("claude", "watching", None)
        except (OSError, ValueError, TypeError) as exc:
            health("claude", "degraded", exc.__class__.__name__)
        try:
            if tail and time.monotonic() >= next_rollout:
                rollout = tail.poll()
                if rollout: publish(rollout)
                next_rollout = time.monotonic() + 5
        except (OSError, ValueError, TypeError) as exc:
            health("rollout", "degraded", exc.__class__.__name__)
        stop.wait(1.0)


def run_antigravity(
    stop: threading.Event,
    publish: Callable[[list[Observation]], None],
    health: Callable[[str, str, str | None], None],
    active: Callable[[], bool],
) -> None:
    """Periodically query Antigravity's local language server for live quota limits."""
    from .config import antigravity_ls_params
    from .providers.antigravity_client import fetch_quota_summary, observations_from_summary

    health("antigravity", "starting", None)
    while not stop.is_set():
        try:
            port, token = antigravity_ls_params()
            if not port or not token:
                health("antigravity", "unavailable", "language server port or csrf token not found")
                if stop.wait(30.0):
                    return
                continue

            summary = fetch_quota_summary(port, token, timeout=5.0)
            if not summary:
                health("antigravity", "degraded", "failed to fetch quota summary")
                if stop.wait(15.0):
                    return
                continue

            observations = observations_from_summary(summary)
            if observations:
                publish(observations)
                health("antigravity", "healthy", None)
            else:
                health("antigravity", "degraded", "empty quota summary")

            poll_interval = 15.0 if active() else 60.0
            if stop.wait(poll_interval):
                return
        except Exception as exc:
            health("antigravity", "degraded", exc.__class__.__name__)
            if stop.wait(15.0):
                return
