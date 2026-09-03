"""Append-only sample store: samples.jsonl plus a latest.json snapshot per window."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Paths
from .util import atomic_write_text, iso, parse_iso, read_json


@dataclass(frozen=True)
class Sample:
    ts: datetime
    provider: str
    window: str
    used: float
    resets_at: datetime | None
    window_min: int
    source: str

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.window}"

    def to_dict(self) -> dict:
        return {
            "ts": iso(self.ts),
            "provider": self.provider,
            "window": self.window,
            "used": round(self.used, 2),
            "resets_at": iso(self.resets_at) if self.resets_at else None,
            "window_min": self.window_min,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Sample | None":
        ts = parse_iso(data.get("ts"))
        if ts is None or not data.get("provider") or not data.get("window"):
            return None
        try:
            used = float(data.get("used"))
            window_min = int(data.get("window_min") or 0)
        except (TypeError, ValueError):
            return None
        return cls(
            ts=ts,
            provider=str(data["provider"]),
            window=str(data["window"]),
            used=used,
            resets_at=parse_iso(data.get("resets_at")),
            window_min=window_min,
            source=str(data.get("source") or ""),
        )

    def same_reading(self, other: "Sample") -> bool:
        return self.used == other.used and self.resets_at == other.resets_at


class _Lock:
    """Exclusive-create lock file; best effort, never blocks for long."""

    def __init__(self, path: Path, timeout: float = 3.0, stale_after: float = 30.0):
        self.path = path
        self.timeout = timeout
        self.stale_after = stale_after
        self.fd: int | None = None

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.stale_after:
                        self.path.unlink()
                        continue
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    self.fd = None
                    return self
                time.sleep(0.05)

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            try:
                self.path.unlink()
            except OSError:
                pass


class Store:
    def __init__(self, paths: Paths):
        self.paths = paths

    # -- read -------------------------------------------------------------
    def latest(self) -> dict[str, Sample]:
        raw = read_json(self.paths.latest, {})
        out: dict[str, Sample] = {}
        if isinstance(raw, dict):
            for key, item in raw.items():
                sample = Sample.from_dict(item) if isinstance(item, dict) else None
                if sample is not None:
                    out[key] = sample
        return out

    def load(self, since: datetime | None = None) -> list[Sample]:
        """All samples (optionally since a time), sorted by time, exact duplicates removed."""
        seen: set[tuple] = set()
        out: list[Sample] = []
        try:
            fh = open(self.paths.samples, encoding="utf-8", errors="replace")
        except OSError:
            return out
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                sample = Sample.from_dict(data) if isinstance(data, dict) else None
                if sample is None or (since is not None and sample.ts < since):
                    continue
                ident = (sample.key, sample.ts, sample.used, sample.resets_at)
                if ident in seen:
                    continue
                seen.add(ident)
                out.append(sample)
        out.sort(key=lambda s: s.ts)
        return out

    # -- write ------------------------------------------------------------
    def append(self, samples: list[Sample], dedupe_s: float = 600.0) -> int:
        """Append samples, skipping repeats of an unchanged reading taken within dedupe_s."""
        if not samples:
            return 0
        with _Lock(self.paths.lock):
            latest = self.latest()
            last_seen = dict(latest)
            lines: list[str] = []
            for sample in sorted(samples, key=lambda s: s.ts):
                prev = last_seen.get(sample.key)
                if prev is not None and prev.same_reading(sample) and abs((sample.ts - prev.ts).total_seconds()) < dedupe_s:
                    continue
                lines.append(json.dumps(sample.to_dict(), separators=(",", ":")))
                if prev is None or sample.ts >= prev.ts:
                    last_seen[sample.key] = sample
                    if sample.key not in latest or sample.ts >= latest[sample.key].ts:
                        latest[sample.key] = sample
            if not lines:
                return 0
            self.paths.samples.parent.mkdir(parents=True, exist_ok=True)
            with open(self.paths.samples, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            self._write_latest(latest)
            return len(lines)

    def prune(self, keep_since: datetime | None = None, drop_source: str | None = None) -> int:
        """Rewrite samples.jsonl without samples older than keep_since or from drop_source.
        Rebuilds latest.json from what remains. Returns rows dropped."""
        with _Lock(self.paths.lock):
            all_samples = self.load()
            kept = [s for s in all_samples if (keep_since is None or s.ts >= keep_since) and (drop_source is None or s.source != drop_source)]
            text = "".join(json.dumps(s.to_dict(), separators=(",", ":")) + "\n" for s in kept)
            atomic_write_text(self.paths.samples, text)
            self._write_latest(self._latest_of(kept))
            return len(all_samples) - len(kept)

    def rebuild_latest(self) -> dict[str, Sample]:
        with _Lock(self.paths.lock):
            latest = self._latest_of(self.load())
            self._write_latest(latest)
            return latest

    @staticmethod
    def _latest_of(samples: list[Sample]) -> dict[str, Sample]:
        latest: dict[str, Sample] = {}
        for sample in samples:  # load() returns time order, so later wins
            if sample.key not in latest or sample.ts >= latest[sample.key].ts:
                latest[sample.key] = sample
        return latest

    def _write_latest(self, latest: dict[str, Sample]) -> None:
        payload = {key: sample.to_dict() for key, sample in sorted(latest.items())}
        atomic_write_text(self.paths.latest, json.dumps(payload, indent=1))
