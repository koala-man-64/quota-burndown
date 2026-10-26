"""Loopback service: poll Azure DevOps on a cadence, re-render, push the change.

Shape borrowed from quota-burndown: one supervised long-running process, a
collector thread with a fixed interval, a cached page served from memory, and a
server-sent-events channel so an open page learns about a new revision instead
of polling for it. The page binds to loopback only - it is a view of one
person's board, not a service.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import ado, publish as publish_mod, refresh, render
from .config import COLLECT_INTERVAL, DEFAULT_PORT, Paths, SUPERVISE_INTERVAL
from .util import WriterLease, atomic_write_text, iso, now_utc, read_json

LOG_MAX_BYTES = 1_000_000
HEARTBEAT_S = 15
# A failed poll should not hammer ADO or the az CLI; back off, then recover.
ERROR_BACKOFF_S = 60


def log(paths: Paths, message: str, detail: str | None = None) -> None:
    """Best-effort bounded logging: the service runs windowless, so this file is
    the only place a failure can announce itself."""
    try:
        path = paths.log
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= LOG_MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{iso(now_utc())} {message}\n")
            if detail:
                handle.write(detail.rstrip() + "\n")
    except OSError:
        pass


def _reachable(host: str, port: int) -> bool:
    connection = http.client.HTTPConnection(host, port, timeout=3)
    try:
        connection.request("GET", "/v1/status")
        response = connection.getresponse()
        response.read()
        return response.status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def supervise(paths: Paths, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
              interval: int = SUPERVISE_INTERVAL, stop_event: threading.Event | None = None) -> None:
    """Keep the service alive without Task Scheduler, for the case where task
    registration was denied and a Startup shortcut is all we have."""
    stop_event = stop_event or threading.Event()
    launcher = Path(__file__).resolve().parent.parent / "ado-swimlane.py"
    command = [sys.executable, "-B", str(launcher), "--home", str(paths.home),
               "serve", "--host", host, "--port", str(port)]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    child = None
    with WriterLease(paths.home, ".supervisor.lock"):
        log(paths, f"supervisor up pid={os.getpid()} interval={interval}s")
        try:
            while not stop_event.is_set():
                if not _reachable(host, port) and (child is None or child.poll() is not None):
                    child = subprocess.Popen(command, creationflags=flags)
                    log(paths, f"supervisor launched service pid={child.pid}")
                stop_event.wait(interval)
        finally:
            log(paths, f"supervisor down pid={os.getpid()}")


class SwimlaneService:
    """Holds the current page and revision, and pushes revisions to listeners."""

    def __init__(self, paths: Paths, *, interval: int = COLLECT_INTERVAL, collect: bool = True,
                 publish: bool = True):
        self.paths = paths
        self.interval = interval
        self.collect = collect
        self.publish_enabled = publish
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.listeners: set[queue.Queue] = set()
        self.lock = threading.Lock()
        self.revision: str | None = None
        self.page: bytes | None = None
        self.status: dict = {"state": "starting", "revision": None, "checked": None, "detail": None}
        self.last_diff: dict | None = None
        self.client = ado.Client()

    # -- state ---------------------------------------------------------------

    def publish(self, result: refresh.RefreshResult) -> None:
        detail = None if result.ok else "; ".join(result.checks.notes)
        page = render.live_page(result.body, result.revision, degraded=detail).encode("utf-8")
        with self.lock:
            self.revision = result.revision
            self.page = page
            self.last_diff = result.board_diff.as_dict()
            self.status = {
                "state": "healthy" if result.ok else "degraded",
                "revision": result.revision,
                "checked": iso(now_utc()),
                "detail": detail,
                "cards": result.checks.totals.get("cards"),
                "openChildren": result.checks.totals.get("openChildren"),
                "changes": len(result.board_diff.changes),
            }
        self.broadcast({"revision": result.revision, "status": self.status["state"]})

    def fail(self, exc: Exception) -> None:
        detail = f"{type(exc).__name__}: {exc}"
        with self.lock:
            self.status = {**self.status, "state": "error", "detail": detail, "checked": iso(now_utc())}
        log(self.paths, "collector error " + detail, traceback.format_exc())
        self.broadcast({"revision": self.revision, "status": "error", "detail": type(exc).__name__})

    def broadcast(self, payload: dict) -> None:
        for listener in list(self.listeners):
            try:
                listener.put_nowait(payload)
            except queue.Full:
                # A listener that cannot keep up will resync on its next connect.
                self.listeners.discard(listener)

    # -- threads -------------------------------------------------------------

    def start(self) -> None:
        if self.collect:
            thread = threading.Thread(target=self._collector, name="collector", daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self) -> None:
        self.stop_event.set()

    def _maybe_publish(self, result: refresh.RefreshResult) -> None:
        """Republish the Artifact when the page would actually look different.

        Never on a page that failed its own checks: a stale published board is
        recoverable, a confidently wrong one that someone shares is not.
        """
        if not self.publish_enabled:
            return
        if not result.ok:
            log(self.paths, "publish skipped: render failed its checks")
            return
        try:
            ok, detail = publish_mod.run_if_due(
                self.paths, result.body, label=publish_mod.label_for(result.revision)
            )
        except Exception as exc:  # publishing must never take the service down
            log(self.paths, f"publish error {type(exc).__name__}: {exc}")
            return
        with self.lock:
            self.status["published"] = detail if ok else None
            self.status["publish_note"] = detail
        log(self.paths, ("published: " if ok else "publish held: ") + detail)

    def _collector(self) -> None:
        while not self.stop_event.is_set():
            wait = self.interval
            try:
                with WriterLease(self.paths.home, ".refresh.lock"):
                    result = refresh.run(self.paths, client=self.client)
                self.publish(result)
                log(self.paths, f"refresh {result.revision} {result.checks.totals.get('openChildren')} open children "
                                f"{len(result.board_diff.changes)} changes ok={result.ok}")
                self._maybe_publish(result)
            except RuntimeError:
                # A manual `refresh` holds the lease; its result lands on disk and
                # the next poll picks it up.
                wait = min(self.interval, 30)
            except Exception as exc:  # collectors must not take the server down
                self.fail(exc)
                wait = ERROR_BACKOFF_S
            self.stop_event.wait(wait)


def loopback_host(host: str) -> str:
    if host == "localhost":
        return "127.0.0.1"
    try:
        if ipaddress.ip_address(host).is_loopback:
            return host
    except ValueError:
        pass
    raise ValueError("the swimlane service must bind to a loopback address")


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(service: SwimlaneService, host: str = "127.0.0.1", port: int = DEFAULT_PORT):
    host = loopback_host(host)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):  # the service log is the only log
            pass

        def _trusted(self) -> bool:
            authority = self.headers.get("Host", "")
            try:
                parsed = urlsplit("http://" + authority)
                valid = parsed.hostname in ("localhost", "127.0.0.1", "::1") and parsed.port == self.server.server_port
            except ValueError:
                valid = False
            origin = self.headers.get("Origin")
            return valid and (origin is None or origin == "http://" + authority)

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._trusted():
                self._send(403, "text/plain", b"loopback origin required")
                return
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                with service.lock:
                    page = service.page
                if page:
                    self._send(200, "text/html; charset=utf-8", page)
                else:
                    self._send(
                        503,
                        "text/html; charset=utf-8",
                        (
                            '<!doctype html><meta http-equiv="refresh" content="3">'
                            '<p style="font:14px system-ui;padding:24px">Pulling the board…</p>'
                        ).encode("utf-8"),
                    )
            elif path == "/v1/status":
                with service.lock:
                    self._send(200, "application/json", json.dumps(service.status).encode())
            elif path == "/v1/changes":
                with service.lock:
                    self._send(200, "application/json", json.dumps(service.last_diff or {}).encode())
            elif path == "/v1/snapshot":
                self._send(200, "application/json", json.dumps(read_json(service.paths.snapshot, {})).encode())
            elif path == "/v1/events":
                self._events()
            else:
                self._send(404, "text/plain", b"not found")

        def _events(self):
            listener: queue.Queue = queue.Queue(maxsize=32)
            service.listeners.add(listener)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                with service.lock:
                    first = {"revision": service.revision, "status": service.status.get("state")}
                self._event(first)
                while not service.stop_event.is_set():
                    try:
                        payload = listener.get(timeout=HEARTBEAT_S)
                    except queue.Empty:
                        # Heartbeat: an idle connection that says nothing is
                        # indistinguishable from a dead service.
                        with service.lock:
                            payload = {"revision": service.revision, "status": service.status.get("state")}
                    self._event(payload)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                service.listeners.discard(listener)

        def _event(self, payload: dict) -> None:
            self.wfile.write(b"data: " + json.dumps(payload).encode() + b"\n\n")
            self.wfile.flush()

    return LocalServer((host, port), Handler)


def serve(paths: Paths, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
          interval: int = COLLECT_INTERVAL, collect: bool = True, publish: bool = True) -> None:
    """Run until interrupted. One writer at a time, enforced by the lease."""
    try:
        lease = WriterLease(paths.home, ".service.lock").__enter__()
    except RuntimeError:
        print(f"a swimlane service is already running; open http://{host}:{port}/")
        return
    service = SwimlaneService(paths, interval=interval, collect=collect, publish=publish)
    server = make_server(service, host, port)
    url = f"http://{host}:{server.server_port}"
    atomic_write_text(paths.service_info, json.dumps({"url": url, "pid": os.getpid(), "started": iso(now_utc())}))
    log(paths, f"service up {url} pid={os.getpid()} interval={interval}s publish={publish}")
    service.start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
        server.shutdown()
        server.server_close()
        if read_json(paths.service_info, {}).get("pid") == os.getpid():
            try:
                paths.service_info.unlink()
            except OSError:
                pass
        log(paths, f"service down pid={os.getpid()}")
        lease.__exit__(None, None, None)


def service_url(paths: Paths) -> str | None:
    info = read_json(paths.service_info, {})
    url = info.get("url")
    if not url:
        return None
    parsed = urlsplit(url)
    return url if _reachable(parsed.hostname or "127.0.0.1", parsed.port or DEFAULT_PORT) else None


def wait_for(paths: Paths, timeout: float = 30.0) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        url = service_url(paths)
        if url:
            return url
        time.sleep(1)
    return None
