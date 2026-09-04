"""Command-line interface."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, install, ledger, render, statusline, usage, usage_report
from .config import DEFAULT_PORT, get_paths
from .model import Burndown, current
from .providers import claude, claude_desktop, codex
from .store import Store
from .util import fmt_local, fmt_minutes, iso, now_utc

LOG_MAX_BYTES = 512_000
PROVIDER_CHOICES = ("all", "claude", "codex", "antigravity")
USAGE_BUDGET_S = {"claude": 10.0}  # a Claude-only collect is what the on-demand skill runs; keep it quick
DEFAULT_USAGE_BUDGET_S = 60.0


def _log(paths, message: str) -> None:
    try:
        if paths.log.exists() and paths.log.stat().st_size > LOG_MAX_BYTES:
            tail = paths.log.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
            paths.log.write_text("\n".join(tail) + "\n", encoding="utf-8")
        with open(paths.log, "a", encoding="utf-8") as fh:
            fh.write(f"{iso(now_utc())} {message}\n")
    except OSError:
        pass


def usage_providers(provider: str) -> list[str]:
    return list(usage.PROVIDERS) if provider == "all" else [provider]


def run_collect(
    paths,
    provider: str = "all",
    debounce_s: float = 0.0,
    since_days: float | None = 14,
    full: bool = False,
    do_render: bool = False,
    usage_on: bool = True,
    usage_since_days: float | None = 30,
    usage_budget_s: float | None = None,
    progress=None,
) -> dict:
    store = Store(paths)
    now = now_utc()
    warnings: list[str] = []
    appended: dict = {}
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
        known = max(
            (s.resets_at for s in store.load(since=now - timedelta(days=8)) if s.provider == "claude" and s.window == "7d" and s.source == "api" and s.resets_at),
            default=None,
        )
        samples, desktop_warnings, stats = claude_desktop.collect(paths.claude_desktop_state, known)
        warnings.extend(desktop_warnings)
        appended["claude_desktop"] = store.append(samples)
        appended["claude_desktop_file"] = stats
    if provider in ("all", "codex"):
        samples, codex_warnings, stats = codex.collect(paths.codex_state, since_days=since_days, full=full)
        warnings.extend(codex_warnings)
        appended["codex"] = store.append(samples)
        appended["codex_files"] = stats
    if usage_on:
        budget = usage_budget_s if usage_budget_s is not None else USAGE_BUDGET_S.get(provider, DEFAULT_USAGE_BUDGET_S)
        try:
            conn = ledger.connect(paths.usage_db)
            try:
                stats, usage_warnings = usage.collect(conn, usage_providers(provider), since_days=usage_since_days, budget_s=budget, progress=progress)
            finally:
                conn.close()
            warnings.extend(usage_warnings)
            appended["usage"] = stats
        except sqlite3.Error as exc:
            warnings.append(f"usage: ledger unavailable ({exc.__class__.__name__}: {exc})")
    if do_render:
        render.write_html(store, paths.html, now=now, warnings=warnings, usage_db=paths.usage_db)
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


def usage_status_lines(paths) -> list[str]:
    try:
        conn = ledger.connect(paths.usage_db)
    except sqlite3.Error as exc:
        return [f"usage: ledger unavailable ({exc.__class__.__name__})"]
    try:
        return usage_report.status_lines(conn)
    finally:
        conn.close()


def cmd_collect(args) -> int:
    paths = get_paths(args.home)
    result = run_collect(
        paths, args.provider, args.debounce, args.since_days, args.full, args.render,
        usage_on=not args.no_usage, usage_since_days=args.usage_since_days, usage_budget_s=args.usage_budget_s,
    )
    if not args.quiet:
        print(json.dumps(result, indent=1))
    return 0


def cmd_backfill(args) -> int:
    paths = get_paths(args.home)
    if not args.usage:
        result = run_collect(paths, "codex", 0, None, True, True, usage_on=False)
        print(json.dumps(result, indent=1))
        return 0
    if args.rescan:
        conn = ledger.connect(paths.usage_db)
        try:
            print(f"forgot {ledger.forget_scans(conn)} scan records; every file will be re-parsed")
        finally:
            conn.close()
    result = run_collect(
        paths, args.provider, do_render=True, usage_on=True, usage_since_days=args.since_days, usage_budget_s=0, progress=print,
    )
    print(json.dumps(result["appended"].get("usage", {}), indent=1))
    for warning in result["warnings"]:
        print(f"warning: {warning}")
    return 0


def cmd_render(args) -> int:
    paths = get_paths(args.home)
    out = render.write_html(Store(paths), args.out or paths.html, days=args.days, usage_db=paths.usage_db)
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
        for line in usage_status_lines(paths):
            print(line)
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


def _dimensions(values: list[str]) -> list[str]:
    requested: list[str] = []
    for value in values:
        requested.extend(part.strip() for part in value.split(",") if part.strip())
    if not requested:
        return list(usage_report.DEFAULT_DIMENSIONS)
    if "all" in requested:
        return list(usage_report.DIMENSIONS)
    unknown = sorted(set(requested) - set(usage_report.DIMENSIONS))
    if unknown:
        raise SystemExit(f"unknown --by dimension(s): {', '.join(unknown)}; choose from {', '.join(usage_report.DIMENSIONS)},all")
    return list(dict.fromkeys(requested))


def cmd_usage(args) -> int:
    paths = get_paths(args.home)
    dimensions = _dimensions(args.by)
    days = None if (args.since or args.until) else args.days
    conn = ledger.connect(paths.usage_db)
    try:
        print(usage_report.render_text(conn, days=days, since=args.since, until=args.until, dimensions=dimensions, raw=args.raw, top=args.top))
        if args.csv or args.json:
            rows = usage_report.export_rows(conn, days=days, since=args.since, until=args.until)
            if args.csv:
                usage_report.write_csv(args.csv, rows)
                print(f"\nwrote {len(rows):,} rows -> {args.csv}")
            if args.json:
                with open(args.json, "w", encoding="utf-8") as fh:
                    json.dump({"generated_at": iso(now_utc()), "filters": {"days": days, "since": args.since, "until": args.until}, "rows": rows}, fh, indent=1)
                print(f"wrote {len(rows):,} rows -> {args.json}")
    finally:
        conn.close()
    return 0


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

    def usage_payload() -> bytes:
        conn = ledger.connect(paths.usage_db)
        try:
            return json.dumps(usage_report.payload(conn), indent=1).encode("utf-8")
        finally:
            conn.close()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                refresh()
                body = render.render_html(store, warnings=state["warnings"], refresh_s=args.refresh, usage_db=paths.usage_db).encode("utf-8")
                self._send(200, "text/html; charset=utf-8", body)
            elif path == "/latest.json":
                body = json.dumps({k: v.to_dict() for k, v in store.latest().items()}, indent=1).encode("utf-8")
                self._send(200, "application/json", body)
            elif path == "/status.json":
                now = now_utc()
                burndowns = current(store.load(since=now - timedelta(days=8)), store.latest(), now)
                self._send(200, "application/json", json.dumps([_bd_json(b) for b in burndowns], indent=1).encode("utf-8"))
            elif path == "/usage.json":
                refresh()
                self._send(200, "application/json", usage_payload())
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
    print(f"usage db:  {paths.usage_db}")
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
    parser = argparse.ArgumentParser(prog="quota-burndown", description="Quota burndown and token usage ledger for Claude Code, Codex and Antigravity.")
    parser.add_argument("--version", action="version", version=f"quota-burndown {__version__}")
    parser.add_argument("--home", help="data directory (default: ~/.quota-burndown or $QUOTA_BURNDOWN_HOME)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("collect", help="sample quotas, ingest new usage, and append to the store")
    p.add_argument("--provider", choices=PROVIDER_CHOICES, default="all")
    p.add_argument("--debounce", type=float, default=0.0, help="skip the Claude API call if a sample newer than this many seconds exists")
    p.add_argument("--since-days", type=float, default=14, help="only scan Codex rollouts modified within N days for quota samples")
    p.add_argument("--full", action="store_true", help="rescan every Codex rollout for quota samples from the beginning")
    p.add_argument("--no-usage", action="store_true", help="skip the per-request usage ledger")
    p.add_argument("--usage-since-days", type=float, default=30, help="only parse usage sources modified within N days")
    p.add_argument("--usage-budget-s", type=float, help="stop parsing usage sources after this many seconds (default 10 for --provider claude, else 60)")
    p.add_argument("--render", action="store_true", help="rewrite the HTML page afterwards")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("backfill", help="one-off history import: Codex quota samples by default, or the usage ledger with --usage")
    p.add_argument("--usage", action="store_true", help="import usage history instead of quota samples")
    p.add_argument("--provider", choices=PROVIDER_CHOICES, default="all", help="usage providers to import (with --usage)")
    p.add_argument("--since-days", type=float, help="with --usage: only sources modified within N days (default: everything, including Codex archives)")
    p.add_argument("--rescan", action="store_true", help="with --usage: forget scan state first so every file is re-parsed")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("render", help="write the HTML page")
    p.add_argument("--out")
    p.add_argument("--days", type=int, default=7)
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("status", help="print the current burndown and today's usage as text")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("usage", help="token usage tables from the ledger")
    p.add_argument("--days", type=int, default=7, help="rolling window when --since/--until are not given (default 7)")
    p.add_argument("--since", help="UTC date YYYY-MM-DD, inclusive")
    p.add_argument("--until", help="UTC date YYYY-MM-DD, inclusive")
    p.add_argument("--by", action="append", default=[], help="comma list of: " + ",".join(usage_report.DIMENSIONS) + ",all")
    p.add_argument("--top", type=int, default=20, help="rows per table")
    p.add_argument("--raw", action="store_true", help="exact numbers instead of K/M")
    p.add_argument("--csv", metavar="PATH", help="also write one row per event")
    p.add_argument("--json", metavar="PATH", help="also write one row per event")
    p.set_defaults(func=cmd_usage)

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
    p.add_argument("--drop-source", choices=["api", "desktop", "statusline", "rollout"], help="remove every sample from this source")
    p.set_defaults(func=cmd_prune)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    args = build_parser().parse_args(argv)
    return args.func(args)
