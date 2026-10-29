"""Read-only backlog collection owned by the dashboard, independent of quota polls."""
from __future__ import annotations

import base64
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from .process_job import child_job
from .util import atomic_write_text, iso, now_utc, read_json

INTERVAL_S = 300
RETRY_S = 60
TIMEOUT_S = 180


class SwimlaneRefresh:
    def __init__(self, *, enabled=True, home=None, launcher=None, clock=now_utc):
        self.home = Path(home or os.environ.get("ADO_SWIMLANE_HOME") or Path.home() / ".ado-swimlane").expanduser()
        self.launcher = Path(launcher or Path(__file__).resolve().parent.parent / "ado-swimlane" / "ado-swimlane.py")
        self.enabled = enabled
        self.clock = clock
        self.lock = threading.Lock()
        self.state = "starting" if enabled else "disabled"
        self.attempt = None
        self.detail = None

    def status(self):
        snapshot = read_json(self.home / "snapshot.json", {}) or {}
        try:
            moment = parsedate_to_datetime(snapshot.get("serverDate") or "").astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            moment = None
        age = (self.clock() - moment).total_seconds() if moment else None
        try:
            rendered = (self.home / "swimlane.html").read_text(encoding="utf-8-sig")
        except OSError:
            rendered = ""
        revision = re.search(r'<meta name="ado-snapshot-at" content="([^"]+)">', rendered)
        rendered_at = revision[1] if revision else None
        with self.lock:
            return {"state": self.state, "attempted_at": self.attempt, "detail": self.detail,
                    "snapshot_at": iso(moment) if moment else None,
                    "age_s": age, "stale": age is None or age < -60 or age > 2 * INTERVAL_S or rendered_at != (iso(moment) if moment else None),
                    "interval_s": INTERVAL_S, "has_rendered": bool(rendered), "rendered_at": rendered_at}

    def _set(self, state, detail=None):
        with self.lock:
            self.state, self.detail = state, detail

    def _redact(self, text):
        secret = os.environ.get("AZURE_DEVOPS_EXT_PAT")
        if secret:
            for value in (secret, base64.b64encode((":" + secret).encode()).decode()):
                text = text.replace(value, "[redacted]")
        return text[-2000:]

    def refresh(self, stop_event):
        # Serialize host refreshes; the child CLI shares .refresh.lock with the
        # standalone service and manual refreshes around the actual provider pull.
        from .service import WriterLease
        lease = WriterLease(self.home, ".dashboard-refresh.lock")
        try:
            lease.__enter__()
        except RuntimeError:
            self._set("busy", "Another backlog refresh holds the writer lease.")
            return RETRY_S
        try:
            with self.lock:
                self.attempt = iso(self.clock())
            self._set("refreshing")
            command = [sys.executable, "-B", str(self.launcher), "--home", str(self.home), "refresh"]
            config_path = self.home / "refresh-config.json"
            config = read_json(config_path, None) if config_path.exists() else {}
            if not isinstance(config, dict):
                raise ValueError("Backlog refresh-config.json must contain a JSON object; retaining last good board.")
            if "template" in config:
                source = Path(config["template"]).expanduser()
                if not source.is_absolute() or not source.is_file():
                    raise ValueError("Configured backlog template must be an existing absolute file; retaining last good board.")
                command.extend(["--template", str(source)])
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, encoding="utf-8", errors="replace", creationflags=flags,
                                  env={**os.environ, "PYTHONIOENCODING": "utf-8"}) as child, child_job(child):
                deadline = time.monotonic() + TIMEOUT_S
                while True:
                    try:
                        output, _ = child.communicate(timeout=1)
                        break
                    except subprocess.TimeoutExpired:
                        if stop_event.is_set() or time.monotonic() >= deadline:
                            child.kill()
                            child.communicate()
                            raise TimeoutError("Backlog refresh stopped or exceeded its deadline.")
                if child.returncode:
                    raise RuntimeError(f"Backlog refresh failed (exit {child.returncode}): {self._redact(output)}")
            status = self.status()
            if not status["has_rendered"] or status["stale"]:
                raise RuntimeError("Refresh did not produce a recent provider snapshot and rendered board.")
            self._set("ok")
            return INTERVAL_S
        except Exception as exc:
            self._set("error", self._redact(f"{type(exc).__name__}: {exc}"))
            return RETRY_S
        finally:
            lease.__exit__(None, None, None)
            atomic_write_text(self.home / "refresh-status.json", json.dumps(self.status()))

    def run(self, stop_event):
        while not stop_event.is_set():
            try:
                wait = self.refresh(stop_event)
            except Exception as exc:  # filesystem failures must not retire the worker
                self._set("error", self._redact(f"{type(exc).__name__}: {exc}"))
                wait = RETRY_S
            stop_event.wait(wait)

    def decorate(self, content: bytes, *, rendered: bool) -> bytes:
        status = self.status()
        text = content.decode("utf-8-sig")
        revision = re.search(r'<meta name="ado-snapshot-at" content="([^"]+)">', text)
        snapshot = revision[1] if rendered and revision else None
        # Bind the visible freshness claim to these bytes, not a snapshot.json
        # that can be replaced just before the rendered HTML is committed.
        visible = {**status, "rendered_at": snapshot, "stale": status["stale"] or snapshot != status["snapshot_at"]}
        label = self.label(visible, rendered)
        banner = ('<div id="ado-refresh-status" role="status" style="padding:10px 16px;'
                  'font:13px system-ui;background:#fff3cd;color:#332701;border-bottom:1px solid #e3cb7d">'
                  + html.escape(label) + '</div>')
        script = """<script>(function(){
var snapshot=__SNAPSHOT__, el=document.getElementById('ado-refresh-status');
function check(){fetch('/v1/swimlane/status',{cache:'no-store'}).then(function(r){if(!r.ok)throw Error();return r.json();}).then(function(s){
  if(s.rendered_at && s.rendered_at!==snapshot){location.reload();return;}
  if(s.rendered_at===snapshot)el.textContent=s.label;
}).catch(function(){el.textContent='Backlog refresh service unavailable; showing the last saved board.';});}
check();setInterval(check,15000);
})();</script>""".replace("__SNAPSHOT__", json.dumps(snapshot))
        if re.search(r"<body\b[^>]*>", text, re.I):
            text = re.sub(r"(<body\b[^>]*>)", lambda m: m[0] + banner, text, count=1, flags=re.I)
        else:
            text = banner + text
        return (text + script).encode("utf-8")

    @staticmethod
    def label(status, rendered=True):
        when = status["rendered_at"] if rendered else None
        if not when:
            return "Backlog unavailable: no verified provider snapshot. Refresh " + status["state"] + "."
        prefix = "Backlog stale" if status["stale"] else "Backlog snapshot"
        note = "Refresh failed; retaining last good board." if status["state"] == "error" else "Refresh " + status["state"] + "."
        return f"{prefix} · {when} · {note} Polls Azure Boards every 5 minutes; quota timestamps are separate."
