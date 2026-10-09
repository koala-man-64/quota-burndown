"""Loopback capacity service: isolated collectors, cached reads, and reconnectable SSE."""
from __future__ import annotations

import ipaddress
import copy
import json
import os
import queue
import threading
import time
from collections import deque
from contextlib import AbstractContextManager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import ledger, render, request_text, usage, usage_report
from .plans import Conflict, PlanState, forecast_snapshot
from .capacity import CapacityState
from .model import current
from .store import Sample, Store
from .util import atomic_write_text, now_utc


class WriterLease(AbstractContextManager):
    """OS-held lock, released on process death; never steal a live writer's lock."""

    def __init__(self, home: Path, filename=".quota-writer.lock"):
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
        except (OSError, IOError):
            self.file.close()
            self.file = None
            raise RuntimeError("quota writer already running; use its capacity endpoint") from None
        return self

    def __exit__(self, *_):
        if self.file:
            self.file.close()
            self.file = None


class CapacityService:
    def __init__(self, paths, *, collectors: bool = True, clock=now_utc):
        self.paths = paths
        self.state = CapacityState(paths.home, clock=clock)
        self.plans = PlanState(paths.home, clock=clock)
        self._credit_forecast = {}
        self._page_wakeup = threading.Event()
        self.store = Store(paths)
        self.stop_event = threading.Event()
        self.events = queue.Queue(maxsize=256)
        self.threads: list[threading.Thread] = []
        self.collectors = collectors
        self._consumers = 0
        self._last_read = 0.0
        self._consumer_lock = threading.Lock()
        self._page = b""
        self._recent_page = (b"", {})
        self._recent_history = deque(maxlen=4)
        self._recent_lock = threading.Lock()
        self._text_slots = threading.BoundedSemaphore(2)
        self._usage = b"{}"
        self._efficiency = b"{}"
        self._legacy_status = b"[]"
        self._legacy_latest = b"{}"
        self._last_persist = 0.0
        self._lease = None
        self.max_publication_latency_ms = 0.0
        self._storage_error_op = None

    def active(self):
        with self._consumer_lock:
            return self._consumers > 0 or time.monotonic() - self._last_read < 60

    def touch(self):
        with self._consumer_lock:
            self._last_read = time.monotonic()

    def _enqueue(self, event):
        try:
            self.events.put_nowait(event)
            return True
        except queue.Full:
            return False

    def publish(self, observations):
        # Bounded backpressure rather than dropping an already accepted policy update.
        while not self.stop_event.is_set():
            try:
                self.events.put(("observations", observations, time.monotonic()), timeout=0.2)
                return
            except queue.Full:
                continue

    def publish_credits(self, observations):
        if not observations:
            return
        while not self.stop_event.is_set():
            try:
                self.events.put(("credits", observations, time.monotonic()), timeout=0.2)
                return
            except queue.Full:
                continue

    def plans_snapshot(self):
        snapshot = self.plans.snapshot()
        cached = self._credit_forecast
        observed = lambda value: {key: obs.get("observed_at") for key, obs in value.get("observations", {}).items()}
        if cached.get("revision") == snapshot["revision"] and observed(cached) == observed(snapshot):
            for provider, plan in snapshot["plans"].items():
                if plan is None:
                    continue
                old = cached.get("plans", {}).get(provider) or {}
                if old.get("remaining") == plan.get("remaining"):
                    plan["forecast"] = copy.deepcopy(old.get("forecast", {"available": False, "note": "Forecast updating."}))
        return snapshot

    def health(self, provider, state, error=None):
        self._enqueue(("health", (provider, state, error), time.monotonic()))

    def _spawn(self, name, target):
        thread = threading.Thread(name=name, target=target, daemon=True)
        self.threads.append(thread)
        thread.start()

    def start(self):
        self._lease = WriterLease(self.paths.home)
        self._lease.__enter__()
        self._spawn("capacity-writer", self._writer)
        # Build pages/usage independently. Never perform this work in HTTP handlers.
        self._spawn("capacity-ledger", self._ledger)
        if self.collectors:
            from .integrations import AdapterContext, run_antigravity, run_claude_oauth, run_codex, run_files
            context = AdapterContext(publish_credits=self.publish_credits)
            self._spawn("capacity-codex", lambda: run_codex(self.stop_event, self.publish, self.health, self.active, context))
            self._spawn("capacity-files", lambda: run_files(self.stop_event, self.publish, self.health, self.paths, context))
            self._spawn("capacity-claude-oauth", lambda: run_claude_oauth(self.stop_event, self.publish, self.health,
                                                                       publish_credits=self.publish_credits))
            self._spawn("capacity-antigravity", lambda: run_antigravity(self.stop_event, self.publish, self.health, self.active))
        return self

    def close(self):
        self.stop_event.set()
        self._page_wakeup.set()
        with self.state.condition:
            self.state.condition.notify_all()
        deadline = time.monotonic() + 20
        for thread in self.threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(t.is_alive() for t in self.threads):
            # Keep ownership until an unusually slow import finishes.
            def release_when_finished():
                for thread in self.threads:
                    thread.join()
                if self._lease:
                    self._lease.__exit__()
                    self._lease = None
            threading.Thread(target=release_when_finished, daemon=True).start()
            return
        if self._lease:
            self._lease.__exit__()
            self._lease = None

    def _writer(self):
        last_tick = 0.0
        while not self.stop_event.is_set():
            received = []
            arrivals = []
            dirty = False
            batch_ok = False
            try:
                event = self.events.get(timeout=0.2)
                batch = [event]
                for _ in range(255):
                    try:
                        batch.append(self.events.get_nowait())
                    except queue.Empty:
                        break
                for kind, data, queued in batch:
                    if kind == "observations":
                        dirty = self.state.ingest(data) or dirty
                        received.extend(self.state.accepted)
                        if self.state.accepted:
                            arrivals.append(queued)
                    elif kind == "health":
                        self.state.set_health(*data)
                        dirty = True
                    elif kind == "policy":
                        self.state.set_policy(data)
                        dirty = True
                    elif kind == "credits":
                        self.plans.ingest(data)
                        self._page_wakeup.set()
                    elif kind == "plan":
                        payload, completed, outcome = data
                        try:
                            outcome.update(status=200, body=self.plans.apply(payload))
                            self._page_wakeup.set()
                        except Conflict as exc:
                            outcome.update(status=409, body={"error": str(exc), "snapshot": self.plans.snapshot()})
                        except (ValueError, TypeError, KeyError) as exc:
                            outcome.update(status=400, body={"error": str(exc)})
                        except OSError:
                            outcome.update(status=503, body={"error": "Could not persist plan; nothing saved."})
                        finally:
                            completed.set()
                batch_ok = True
            except queue.Empty:
                pass
            except (OSError, ValueError, RuntimeError, TypeError) as exc:
                self.state.set_health("ingest", "error", type(exc).__name__)
                dirty = True
            if batch_ok and self.state.health.get("ingest", {}).get("state") == "error":
                self.state.set_health("ingest", "healthy", None)
            now = time.monotonic()
            if arrivals:
                latency = (now - min(arrivals)) * 1000
                self.max_publication_latency_ms = max(self.max_publication_latency_ms, latency)
                self.state.health["publication"] = {"state": "healthy", "last_latency_ms": latency,
                    "max_latency_ms": self.max_publication_latency_ms, "target_ms": 2000,
                    "measurement": "adapter enqueue to snapshot assembly; upstream delay excluded"}
            if dirty or now - last_tick >= 0.5:
                self.state.publish(persist=False)
                last_tick = now
            if now - self._last_persist >= 2:
                try:
                    self.state.publish(persist=True)
                    self._last_persist = now
                    if self._storage_error_op in ("persist", None):
                        self._storage_error_op = None
                        if self.state.health.get("storage", {}).get("state") == "error":
                            self.state.set_health("storage", "healthy", None)
                except (OSError, ValueError, RuntimeError) as exc:
                    self._storage_error_op = "persist"
                    self.state.set_health("storage", "error", type(exc).__name__)
                    self.state.publish()
            if received:
                samples = [Sample(i.observed_at, i.provider, f"{i.window}:{i.limit_id}", i.used_pct, i.resets_at,
                                  i.window_min, i.source) for i in received if i.used_pct is not None]
                try:
                    self.store.append(samples)
                    self._cache_legacy()
                    if self._storage_error_op in ("append", None):
                        self._storage_error_op = None
                        if self.state.health.get("storage", {}).get("state") == "error":
                            self.state.set_health("storage", "healthy", None)
                except (OSError, ValueError, RuntimeError) as exc:
                    self._storage_error_op = "append"
                    self.state.set_health("storage", "error", type(exc).__name__)
                    self.state.publish()

    def _cache_legacy(self):
        from .cli import _bd_json
        latest = self.store.latest()
        self._legacy_latest = json.dumps({k: v.to_dict() for k, v in latest.items()}).encode()
        self._legacy_status = json.dumps([_bd_json(b) for b in current([], latest, now_utc())]).encode()

    def _publish_page(self, page, rows):
        # A browser can still be displaying the preceding snapshot when collection
        # publishes the next one. Keep a bounded grace period for its text links.
        with self._recent_lock:
            self._recent_history.append((time.monotonic(), self._recent_page[1]))
            self._recent_page = (page, rows)
        self._page = page

    def _recent_row(self, row_id):
        with self._recent_lock:
            row = self._recent_page[1].get(row_id)
            if row is not None:
                return row
            for published, rows in reversed(self._recent_history):
                if time.monotonic() - published <= 120 and row_id in rows:
                    return rows[row_id]
        return None

    def _ledger(self):
        while not self.stop_event.is_set():
            try:
                if self.collectors:
                    try:
                        with WriterLease(self.paths.home, ".usage-writer.lock"):
                            conn = ledger.connect(self.paths.usage_db)
                            try:
                                usage.collect(conn, budget_s=15)
                            finally:
                                conn.close()
                    except RuntimeError:
                        # An explicit backfill may hold the independent ledger lease.
                        pass
                conn = ledger.connect(self.paths.usage_db)
                now = now_utc()
                try:
                    self._usage = json.dumps(usage_report.payload(conn, now)).encode()
                    efficiency = usage_report.efficiency_payload(conn, now)
                    efficiency["daily_models_html"] = render.daily_models_html(efficiency["daily_models"])
                    self._efficiency = json.dumps(efficiency).encode()
                    recent = ledger.recent_requests(conn)
                    credit_plans = forecast_snapshot(self.plans.snapshot(), conn,
                        self.store.load(since=now - timedelta(days=7)), self.state.read(), now)
                    self._credit_forecast = credit_plans
                finally:
                    conn.close()
                page = render.render_html(self.store, now=now, usage_db=self.paths.usage_db,
                                          capacity=self.state.read(), live=True, recent_rows=recent,
                                          credit_plans=credit_plans).encode("utf-8")
                self._publish_page(page, {request_text.row_id(row): dict(row) for row in recent})
                self._cache_legacy()
                self.health("ledger", "healthy")
            except Exception as exc:
                self.health("ledger", "error", type(exc).__name__)
            if self.stop_event.wait(0):
                break
            self._page_wakeup.wait(30)
            self._page_wakeup.clear()


def loopback_host(host: str) -> str:
    if host == "localhost":
        return "127.0.0.1"
    try:
        if ipaddress.ip_address(host).is_loopback:
            return host
    except ValueError:
        pass
    raise ValueError("capacity service must bind to a loopback address")


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(service: CapacityService, host="127.0.0.1", port=8787):
    host = loopback_host(host)
    if ":" in host:
        import socket
        class Server(LocalServer):
            address_family = socket.AF_INET6
    else:
        Server = LocalServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _trusted(self):
            authority = self.headers.get("Host", "")
            try:
                parsed = urlsplit("http://" + authority)
                valid_host = parsed.hostname in ("localhost", "127.0.0.1", "::1") and parsed.port == self.server.server_port
            except ValueError:
                valid_host = False
            origin = self.headers.get("Origin")
            return valid_host and (origin is None or origin == "http://" + authority)

        def _send(self, code, ctype, body):
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
            if path == "/v1/capacity":
                service.touch()
                self._send(200, "application/json", json.dumps(service.state.read()).encode())
            elif path == "/v1/plans":
                service.touch()
                self._send(200, "application/json", json.dumps(service.plans_snapshot()).encode())
            elif path == "/v1/capacity/events":
                self._events()
            elif path in ("/", "/index.html"):
                service.touch()
                page = service._recent_page[0] or service._page
                if page:
                    self._send(200, "text/html; charset=utf-8", page)
                else:
                    self._send(503, "text/html; charset=utf-8", b'<!doctype html><meta http-equiv="refresh" content="2"><p>Preparing the dashboard. Capacity API is available now.</p>')
            elif path == "/usage.json":
                self._send(200, "application/json", service._usage)
            elif path == "/v1/usage":
                self._send(200, "application/json", service._efficiency)
            elif path.startswith('/v1/recent-text/'):
                row = service._recent_row(path.removeprefix('/v1/recent-text/'))
                if row is None:
                    self._send(404, 'application/json', b'{"status":"unavailable","note":"This request is no longer in the displayed recent list. Reload the dashboard."}')
                elif not service._text_slots.acquire(blocking=False):
                    self._send(503, 'application/json', b'{"status":"unavailable","note":"Text viewer is busy. Close and reopen to retry."}')
                else:
                    try:
                        result = request_text.read_request(row)
                        self._send(200, 'application/json; charset=utf-8', json.dumps(result).encode())
                    finally:
                        service._text_slots.release()
            elif path == "/latest.json":
                self._send(200, "application/json", service._legacy_latest)
            elif path == "/status.json":
                self._send(200, "application/json", service._legacy_status)
            else:
                self._send(404, "text/plain", b"not found")

        def do_POST(self):
            if not self._trusted():
                self._send(403, "text/plain", b"loopback origin required")
                self.close_connection = True
                return
            if self.path == "/v1/plans":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4096 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        raise ValueError("bounded JSON request required")
                    self.connection.settimeout(2)
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError("plan request must be an object")
                    completed, outcome = threading.Event(), {}
                    if not service._enqueue(("plan", (payload, completed, outcome), time.monotonic())):
                        self._send(503, "application/json", b'{"error":"writer queue full; retry"}')
                    elif completed.wait(2):
                        self._send(outcome["status"], "application/json", json.dumps(outcome["body"]).encode())
                    else:
                        self._send(202, "application/json", b'{"accepted":true,"note":"Save queued; reload plans to confirm."}')
                except (ValueError, TypeError, KeyError, OSError):
                    self._send(400, "application/json", b'{"error":"bounded JSON plan request required"}')
                self.close_connection = True
                return
            if self.path != "/v1/policy":
                self._send(404, "text/plain", b"not found")
                self.close_connection = True
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError()
                self.connection.settimeout(2)
                payload = json.loads(self.rfile.read(length))
                reserve = payload["reserve_pct"]
                if type(reserve) is not int or reserve not in (5, 10, 20):
                    raise ValueError()
                if service._enqueue(("policy", reserve, time.monotonic())):
                    self._send(202, "application/json", b'{"accepted":true}')
                else:
                    self._send(503, "application/json", b'{"error":"writer queue full; retry"}')
            except (ValueError, TypeError, KeyError, OSError):
                self._send(400, "application/json", b'{"error":"reserve_pct must be 5, 10, or 20"}')
                self.close_connection = True

        def _events(self):
            with service._consumer_lock:
                if service._consumers >= 32:
                    self._send(503, "text/plain", b"too many event consumers")
                    return
                service._consumers += 1
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.connection.settimeout(5)
                snapshot = service.state.read()
                sent_revision = None
                while not service.stop_event.is_set():
                    if sent_revision != snapshot["revision"]:
                        data = json.dumps(snapshot, separators=(",", ":"))
                        self.wfile.write(f"id: {snapshot['instance_id']}:{snapshot['revision']}\ndata: {data}\n\n".encode())
                        sent_revision = snapshot["revision"]
                    else:
                        self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    snapshot = service.state.wait(snapshot["revision"], timeout=15)
            except (OSError, ConnectionError):
                pass
            finally:
                self.close_connection = True
                with service._consumer_lock:
                    service._consumers -= 1

        def log_message(self, *_):
            pass

    return Server((host, port), Handler)


def serve(paths, host="127.0.0.1", port=8787):
    host = loopback_host(host)
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    service = None
    server = None
    try:
        # Serialize replacement through metadata publication. Never delete or
        # steal the writer lock, and never kill an arbitrary port occupant.
        with WriterLease(paths.home, ".quota-start.lock"):
            try:
                with WriterLease(paths.home):
                    pass
            except RuntimeError:
                from .restart import stop_existing
                stop_existing(paths.home)
            service = CapacityService(paths)
            server = make_server(service, host, port)
            service.start()
            authority = f"[{server.server_address[0]}]" if ":" in server.server_address[0] else server.server_address[0]
            from .restart import current_process_started
            atomic_write_text(paths.home / "capacity-service.json", json.dumps({
                "url": f"http://{authority}:{server.server_port}", "pid": os.getpid(),
                "started": current_process_started(),
            }))
        print(f"serving http://{host}:{server.server_port}/ (Ctrl+C to stop)", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            server.server_close()
        if service is not None:
            service.close()
