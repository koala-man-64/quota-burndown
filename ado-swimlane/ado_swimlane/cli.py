"""Command line: refresh, serve, install, and the checks that gate publishing."""
from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from pathlib import Path

from . import (
    diff,
    install as install_mod,
    publish as publish_mod,
    refresh as refresh_mod,
    service as service_mod,
    validate,
)
from .config import COLLECT_INTERVAL, DEFAULT_PORT, get_paths, template_path
from .util import read_json


def _run_refresh(args) -> int:
    paths = get_paths(args.home)
    try:
        result = refresh_mod.run(paths, source=Path(args.template) if args.template else None)
    except RuntimeError as exc:
        print(f"refresh skipped: {exc}", file=sys.stderr)
        return 1
    print(f"revision {result.revision}")
    print(f"  injected {result.report.summary()}")
    for dom_id, before, after in result.report.states:
        print(f"    state {dom_id}: {before} -> {after}")
    for dom_id, styling, live in result.report.mismatches:
        # Not an error: the card's styling carries a curated claim that only a
        # person should change. Reported so the next prose pass can settle it.
        print(f"    styling {dom_id}: shown as {styling}, live state {live}")
    for line in result.board_diff.lines():
        print(line)
    print(str(result.checks))
    print(f"  wrote {paths.artifact}")
    print(f"  wrote {paths.html}")
    return 0 if result.ok else 2


def _run_serve(args) -> int:
    paths = get_paths(args.home)
    if args.supervise:
        service_mod.supervise(paths, host=args.host, port=args.port)
        return 0
    service_mod.serve(paths, host=args.host, port=args.port, interval=args.interval,
                      collect=not args.no_collect, publish=not args.no_publish)
    return 0


def _run_publish(args) -> int:
    paths = get_paths(args.home)
    if not paths.artifact.exists():
        print(f"{paths.artifact} does not exist; run `refresh` first", file=sys.stderr)
        return 1
    body = paths.artifact.read_text(encoding="utf-8-sig")
    state = publish_mod.PublishState.load(paths)
    decision = publish_mod.decide(state, body, force=args.force)
    print(f"decision: {'publish' if decision.publish else 'hold'} - {decision.reason}")
    if not decision.publish or args.dry_run:
        if args.dry_run and decision.publish:
            print("dry run: nothing published")
        return 0
    revision = read_json(paths.snapshot, {}).get("serverDate") or ""
    label = args.label or publish_mod.label_for(refresh_mod.render.revision_of({"serverDate": revision}))
    ok, detail = publish_mod.run_if_due(paths, body, label=label, force=args.force)
    print(("published: " if ok else "not published: ") + detail)
    return 0 if ok else 2


def _run_open(args) -> int:
    paths = get_paths(args.home)
    url = service_mod.service_url(paths)
    if not url:
        launcher = Path(__file__).resolve().parent.parent / "ado-swimlane.py"
        print("no service running. Start one with:")
        print(f"  py {launcher} serve")
        print(f"or install it to start at logon:  py {launcher} install --apply")
        return 1
    print(url)
    if not args.no_browser:
        webbrowser.open(url)
    return 0


def _run_status(args) -> int:
    paths = get_paths(args.home)
    url = service_mod.service_url(paths)
    print(f"home:      {paths.home}")
    print(f"template:  {template_path()}")
    print(f"service:   {url or 'not running'}")
    snapshot = read_json(paths.snapshot, {})
    if snapshot:
        print(f"snapshot:  {snapshot.get('serverDate')} "
              f"({len(snapshot.get('items') or [])} items, "
              f"{len(snapshot.get('children') or [])} open children of cards)")
    else:
        print("snapshot:  none yet")
    state = publish_mod.PublishState.load(paths)
    if state.published_at:
        print(f"published: {state.published_at} ({state.count_today} today"
              f"{'' if state.bootstrapped else ', session needs bootstrapping'})")
    else:
        print("published: never")
    if state.last_error:
        # A jam is the designed response to an out-of-band publish, so say how
        # to clear it rather than leaving it looking like a crash.
        print(f"  held:    {state.last_error[:160]}")
        print("  clear with: ado-swimlane.py publish --force  (re-establishes the baseline)")
    print(install_mod.status())
    return 0


def _run_validate(args) -> int:
    paths = get_paths(args.home)
    path = Path(args.file) if args.file else paths.artifact
    if not path.exists():
        print(f"{path} does not exist; run `refresh` first", file=sys.stderr)
        return 1
    snapshot = read_json(paths.snapshot) if args.against_live else None
    expected = len(snapshot.get("children") or []) if snapshot else None
    result = validate.check(path.read_text(encoding="utf-8-sig"), snapshot, expected_children=expected)
    print(json.dumps(result.totals, indent=2))
    print(str(result))
    return 0 if result.ok else 2


def _run_changes(args) -> int:
    paths = get_paths(args.home)
    if not paths.changes.exists():
        print("no change log yet; run `refresh` first", file=sys.stderr)
        return 1
    lines = paths.changes.read_text(encoding="utf-8").splitlines()
    for raw in lines[-args.limit:]:
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        record = diff.BoardDiff(
            since=payload.get("since"),
            until=payload.get("until"),
            changes=[diff.Change(**c) for c in payload.get("changes", [])],
            children_added={int(k): v for k, v in (payload.get("children_added") or {}).items()},
            children_gone={int(k): v for k, v in (payload.get("children_gone") or {}).items()},
            unparented=payload.get("unparented") or [],
            resolved_not_closed=payload.get("resolved_not_closed") or [],
            counts=payload.get("counts") or {},
        )
        print("\n".join(record.lines()))
        print()
    return 0


def _run_install(args) -> int:
    print(install_mod.install(apply=args.apply, home=args.home, port=args.port))
    if args.apply:
        paths = get_paths(args.home)
        url = service_mod.wait_for(paths, timeout=45)
        print(f"service: {url}" if url else "service has not answered yet; check `status` in a minute")
    return 0


def _run_uninstall(args) -> int:
    print(install_mod.uninstall(apply=args.apply))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ado-swimlane", description=__doc__)
    parser.add_argument("--home", help="state directory (default ~/.ado-swimlane)")
    sub = parser.add_subparsers(dest="command", required=True)

    one = sub.add_parser("refresh", help="pull the board once, re-render, verify")
    one.add_argument("--template", help="curated board to inject into (default board/board.html)")
    one.set_defaults(func=_run_refresh)

    serve = sub.add_parser("serve", help="run the loopback service (polls and pushes updates)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--interval", type=int, default=COLLECT_INTERVAL, help="seconds between pulls")
    serve.add_argument("--no-collect", action="store_true", help="serve the last render without polling")
    serve.add_argument("--no-publish", action="store_true", help="never republish the Artifact")
    serve.add_argument("--supervise", action="store_true", help="keep a serve process alive (autostart fallback)")
    serve.set_defaults(func=_run_serve)

    pub = sub.add_parser("publish", help="republish the Artifact if the page would look different")
    pub.add_argument("--force", action="store_true", help="publish even if unchanged or debounced")
    pub.add_argument("--dry-run", action="store_true", help="say what would happen, publish nothing")
    pub.add_argument("--label", help="Artifact label (60 characters max)")
    pub.set_defaults(func=_run_publish)

    show = sub.add_parser("open", help="print (and open) the running service URL")
    show.add_argument("--no-browser", action="store_true")
    show.set_defaults(func=_run_open)

    sub.add_parser("status", help="where everything is and whether it is running").set_defaults(func=_run_status)

    check = sub.add_parser("validate", help="re-run the structural and numeric checks")
    check.add_argument("--file", help="page to check (default the last rendered artifact)")
    check.add_argument("--against-live", action="store_true", help="also compare card states to the last snapshot")
    check.set_defaults(func=_run_validate)

    changes = sub.add_parser("changes", help="replay recent change reports")
    changes.add_argument("--limit", type=int, default=1)
    changes.set_defaults(func=_run_changes)

    setup = sub.add_parser("install", help="start the service at logon, with a watchdog")
    setup.add_argument("--apply", action="store_true", help="actually install (default is a dry run)")
    setup.add_argument("--port", type=int, default=DEFAULT_PORT)
    setup.set_defaults(func=_run_install)

    remove = sub.add_parser("uninstall", help="remove autostart")
    remove.add_argument("--apply", action="store_true")
    remove.set_defaults(func=_run_uninstall)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
