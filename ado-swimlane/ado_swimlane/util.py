"""Small shared helpers: clocks, atomic writes, and a single-writer lease."""
from __future__ import annotations

import json
import os
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then replace.

    A reader (the served page, a Claude refresh, the next poll) must never see a
    half-written snapshot or a truncated page.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        # A BOM here is not hypothetical: anything a PowerShell step wrote with
        # Out-File -Encoding utf8 carries one, and json.loads rejects it.
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, payload) -> None:
    atomic_write_text(path, json.dumps(payload, separators=(",", ":")))


class WriterLease(AbstractContextManager):
    """OS-held lock released on process death, so a crashed writer never wedges
    the next one and two overlapping collectors never race the same snapshot."""

    def __init__(self, home: Path, filename: str = ".writer.lock"):
        self.path = home / filename
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.file.seek(0)
                if self.file.read(1) == b"":
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise RuntimeError("another ado-swimlane writer holds the lease") from None
        return self

    def __exit__(self, *_):
        if self.file:
            self.file.close()
            self.file = None
