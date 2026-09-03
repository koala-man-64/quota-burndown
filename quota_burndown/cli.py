"""Command-line interface."""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, install, render, statusline
from .config import DEFAULT_PORT, get_paths
from .model import Burndown, current
from .providers import claude, codex
from .store import Store
from .util import fmt_local, fmt_minutes, iso, now_utc

LOG_MAX_BYTES = 512_000


def _log(paths, message: str) -> None:
    try:
        if paths.log.exists() and paths.log.stat().st_size > LOG_MAX_BYTES:
            tail = paths.log.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
            paths.log.write_text("\n".join(tail) + "\n", encoding="utf-8")
        with open(paths.log, "a", encoding="utf-8") as fh:
            fh.write(f"{iso(now_utc())} {message}\n")
    except OSError:
        pass


def run_collect(paths, provider: str = "all", debounce_s: float = 0.0, since_days: float = 14, full: bool = False, do_render: bool = False) -> dict:
    store = Store(paths)
    now = now_utc()
    warnings: list[str] = []
    appended = {}
    if provider in ("all", "claude"):
        latest = store.latest()
        fresh = [s for s in latest.values() if s.provider == "claude" and s.source == "api" and (now - s.ts).total_seconds() < debounce_s]
        if debounce_s and fresh:
            appended["claude"] = "debounced"
        else:
            samples, warning = claude.collect(now)
            if warning:
                warnings.append(warning)
            appended["claude"] = store.append(samples)
    if provider in ("all", "codex"):
        samples, codex_warnings, stats = codex.collect(paths.codex_state, since_days=since_days, full=full)
        warnings.extend(codex_warnings)
        appended["codex"] = store.append(samples)
        appended["codex_files"] = stats
    if do_render:
        render.write_html(store, paths.html, now=now, warnings=warnings)
    _log(paths, f"collect provider={provider} appended={json.dumps(appended)} warnings={len(warnings)}" + (" " + " | ".join(warnings) if warnings else ""))
    return {"appended": appended, "warnings": warnings}


def status_lines(burndowns: list[Burndown]) -> list[str]:
    lines = []
    for bd in burndowns:
        title = f"{bd.provider.capitalize()} {render.window_title(bd.window)}"
        if bd.status == "idle":
            lines.append(f"{title}: {bd.used:.0f}% used, no active window")
            continue
        resets = fmt_local(bd.resets_at) if bd.resets_at else "?"
        detail = f"{title}: {bd.used:.0f}% used vs {bd.pace:.0f}% pace ({render.badge_text(bd)}); {fmt_minutes(bd.remaining_min)} left, resets {resets}"
        value, label = render.projection_text(bd)
        if value != "—":
            detail += f"; {value} {label}"
        if bd.age_min is not None and bd.age_min > render.STALE_MIN:
            detail += f" [stale: last sample {bd.age_min:.0f} min ago]"
        lines.append(detail)
    return lines or ["no samples yet; run: quota-burndown collect"]


def cmd_collect(args) -> int:
    paths = get_paths(args.home)
    result = run_collect(paths, args.provider, args.debounce, args.since_days, args.full, args.render)
    if not args.quiet:
        print(json.dumps(result, indent=1))
    return 0


def cmd_render(args) -> int:
    paths = get_paths(args.home)
    out = render.write_html(Store(paths), args.out or paths.html, days=args.days)
    print(out)
    return 0


def cmd_status(args) -> int:
    paths = get_paths(args.home)
    store = Store(paths)
    now = now_utc()
    samples = store.load(since=now - timedelta(days=8))
    burndowns = current(samples, store.latest(), now)
    if args.json:
        print(json.dumps([_bd_json(bd) for bd in burndowns], indent=1))
    else:
        print("\n".join(status_lines(burndowns)))
        print(f"page: {paths.html}")
    return 0


def _bd_json(bd: Burndown) -> dict:
    return {
        "provider": bd.provider, "window": bd.window, "status": bd.status, "used": round(bd.used, 1),
        "pace": round(bd.pace, 1), "delta": round(bd.delta, 1), "remaining_min": round(bd.remaining_min),
        "resets_at": iso(bd.resets_at) if bd.resets_at else None,
        "exhaust_at": iso(bd.exhaust_at) if bd.exhaust_at else None,
        "projected_end": round(bd.projected_end, 1), "sample_ts": iso(bd.sample_ts) if bd.sample_ts else None,
        "source": bd.source, "samples": len(bd.samples),
    }


def cmd_statusline(args) -> int:
    paths = get_paths(args.home)
    try:
        text = sys.stdin.read()
    except OSError:
        text = ""
    try:
        line = statusline.run(text, Store(paths), color=not args.no_color)
    except Exception as exc:  # never break the status bar
        line = f"quota: error ({exc.__class__.__name__})"
    print(line)
    return 0


def cmd_serve(args) -> int:
    paths = get_paths(args.home)
    store = Store(paths)
    state = {"last_collect": 0.0, "warnings": []}
    lock = threading.Lock()

    def refresh() -> None:
        with lock:
            if time.monotonic() - state["last_collect"] < args.min_interval:
                return
            result = run_collect(paths, "all", debounce_s=args.min_interval)
            state["warnings"] = result["warnings"]
            state["last_collect"] = time.monotonic()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                refresh()
                body = render.render_html(store, warnings=state["warnings"], refresh_s=args.refresh).encode("utf-8")
                self._send(200, "text/html; charset=utf-8", body)
            elif path == "/latest.json":
                body = json.dumps({k: v.to_dict() for k, v in store.latest().items()}, indent=1).encode("utf-8")
                self._send(200, "application/json", body)
            elif path == "/status.json":
                now = now_utc()
                burndowns = current(store.load(since=now - timedelta(days=8)), store.latest(), now)
                self._send(200, "application/json", json.dumps([_bd_json(b) for b in burndowns], indent=1).encode("utf-8"))
            else:
                self._send(404, "text/plain", b"not found")

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            return

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"serving http://{args.host}:{args.port}/  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_install(args) -> int:
    apply = args.apply
    wants_all = args.all or not (args.statusline or args.task or args.codex_skill)
    lines = []
    if wants_all or args.statusline:
        lines.append(install.install_statusline(apply=apply, force=args.force))
    if wants_all or args.task:
        lines.append(install.install_task(apply=apply, every_min=args.every))
    if wants_all or args.codex_skill:
        lines.append(install.install_codex_skill(apply=apply))
    lines.append(install.plugin_instructions())
    if not apply:
        lines.append("(dry run; add --apply to make these changes)")
    print("\n".join(lines))
    return 0


def cmd_uninstall(args) -> int:
    lines = [install.uninstall_statusline(apply=args.apply), install.uninstall_task(apply=args.apply)]
    if not args.apply:
        lines.append("(dry run; add --apply to make these changes)")
    print("\n".join(lines))
    return 0


def cmd_where(args) -> int:
    paths = get_paths(args.home)
    print(f"home:      {paths.home}")
    print(f"samples:   {paths.samples}")
    print(f"page:      {paths.html}")
    print(f"log:       {paths.log}")
    print(f"launcher:  {install.launcher_path()}")
    print(f"task:      {install.task_status()}")
    return 0


def cmd_prune(args) -> int:
    paths = get_paths(args.home)
    keep_since = now_utc() - timedelta(days=args.keep_days) if args.keep_days else None
    dropped = Store(paths).prune(keep_since=keep_since, drop_source=args.drop_source)
    print(f"dropped {dropped} samples; latest.json rebuilt")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quota-burndown", description="Burndown of Claude and Codex subscription quotas.")
    parser.add_argument("--version", action="version", version=f"quota-burndown {__version__}")
    parser.add_argument("--home", help="data directory (default: ~/.quota-burndown or $QUOTA_BURNDOWN_HOME)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("collect", help="sample quotas and append to the store")
    p.add_argument("--provider", choices=["all", "claude", "codex"], default="all")
    p.add_argument("--debounce", type=float, default=0.0, help="skip the Claude API call if a sample newer than this many seconds exists")
    p.add_argument("--since-days", type=float, default=14, help="only scan Codex rollouts modified within N days")
    p.add_argument("--full", action="store_true", help="rescan every Codex rollout from the beginning")
    p.add_argument("--render", action="store_true", help="rewrite the HTML page afterwards")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("backfill", help="scan every Codex rollout ever written (one-off history import)")
    p.set_defaults(func=lambda a: cmd_collect(argparse.Namespace(home=a.home, provider="codex", debounce=0, since_days=None, full=True, render=True, quiet=False)))

    p = sub.add_parser("render", help="write the HTML page")
    p.add_argument("--out")
    p.add_argument("--days", type=int, default=7)
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("status", help="print the current burndown as text")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("statusline", help="Claude Code statusLine entry point (reads JSON on stdin)")
    p.add_argument("--no-color", action="store_true")
    p.set_defaults(func=cmd_statusline)

    p = sub.add_parser("serve", help="local web page that re-collects on each load")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--min-interval", type=float, default=60, help="seconds between collections triggered by page loads")
    p.add_argument("--refresh", type=int, default=120, help="page auto-reload seconds")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("install", help="wire up the status line, scheduled task, and Codex skill (dry run by default)")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--all", action="store_true")
    p.add_argument("--statusline", action="store_true")
    p.add_argument("--task", action="store_true")
    p.add_argument("--codex-skill", action="store_true")
    p.add_argument("--force", action="store_true", help="replace an existing statusLine that is not ours")
    p.add_argument("--every", type=int, default=5, help="scheduled task interval in minutes")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("uninstall", help="remove the status line and scheduled task (dry run by default)")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("where", help="print data locations and task status")
    p.set_defaults(func=cmd_where)

    p = sub.add_parser("prune", help="drop samples older than N days and/or from one source; rebuilds latest.json")
    p.add_argument("--keep-days", type=int, default=90, help="0 keeps everything regardless of age")
    p.add_argument("--drop-source", choices=["api", "statusline", "rollout"], help="remove every sample from this source")
    p.set_defaults(func=cmd_prune)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    args = build_parser().parse_args(argv)
    return args.func(args)
