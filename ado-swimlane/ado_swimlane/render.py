"""Turn the curated template plus a live snapshot into a servable page.

Three flavours, all from the same injected body:

  live=True     loopback page: a server-sent-events channel reloads it the moment
                a poll produces a new revision
  static        standalone file: no server behind it, so it reloads itself on a
                timer instead
  artifact      no refresh wiring at all - the published Artifact is a snapshot
                that a Claude session republishes, and a meta-refresh there would
                reload a page that cannot have changed
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from . import template
from .config import STATIC_REFRESH
from .util import iso, now_utc

# The Artifact host wraps a published fragment in exactly this shell. Repeating
# it locally means the loopback page is a faithful preview of what gets
# published, rather than an unstyled approximation.
HEAD = (
    '<!doctype html><html><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    "<style>:root{color-scheme:light}body{margin:0;padding:0;"
    "font:14px -apple-system,BlinkMacSystemFont,sans-serif;background:#faf9f5;color:#141413}"
    "img{max-width:100%}[hidden]:not([hidden=until-found i]){display:none!important}</style>"
)
TAIL = "</body></html>"

LIVE_STYLE = """
<style>
  .live-chip {
    position: fixed; right: 12px; bottom: 12px; z-index: 40;
    display: flex; align-items: center; gap: 7px;
    font: 11.5px/1.2 "Segoe UI", system-ui, sans-serif;
    background: var(--panel, #fff); color: var(--ink-soft, #4c5a66);
    border: 1px solid var(--line, #d5dde2); border-radius: 999px;
    padding: 5px 11px 5px 9px; box-shadow: 0 2px 10px rgba(0,0,0,0.12);
  }
  .live-chip i { width: 7px; height: 7px; border-radius: 50%; background: var(--ok, #2e7d4f); display: block; }
  .live-chip.stale i { background: var(--amber, #a06b00); }
  .live-chip.down i { background: var(--danger, #b3382e); }
  @media print { .live-chip { display: none; } }
</style>
"""

# Reload on a new revision rather than patching the DOM: every derived view on
# this page (badges, arrows, the work breakdown) is already computed from the
# DOM at load, so a reload is the one update path that cannot leave two views
# disagreeing. Scroll position and open panels are carried across it.
LIVE_SCRIPT = """
<script>
(function () {
  var REVISION = "__REVISION__";
  var KEY = "swimlane.view";
  try {
    var saved = JSON.parse(sessionStorage.getItem(KEY) || "null");
    if (saved) {
      (saved.open || []).forEach(function (id) {
        var el = document.getElementById(id);
        if (el && el.tagName === "DETAILS") el.open = true;
      });
      (saved.closed || []).forEach(function (id) {
        var el = document.getElementById(id);
        if (el && el.tagName === "DETAILS") el.open = false;
      });
      if (saved.y) window.scrollTo(0, saved.y);
      sessionStorage.removeItem(KEY);
    }
  } catch (e) { /* a blocked sessionStorage must not stop the page */ }

  function remember() {
    try {
      var open = [], closed = [];
      document.querySelectorAll("details[id]").forEach(function (d) {
        (d.open ? open : closed).push(d.id);
      });
      sessionStorage.setItem(KEY, JSON.stringify({ y: window.scrollY, open: open, closed: closed }));
    } catch (e) { /* ignore */ }
  }

  var chip = document.createElement("div");
  chip.className = "live-chip";
  var dot = document.createElement("i");
  var label = document.createElement("span");
  chip.appendChild(dot); chip.appendChild(label);
  document.body.appendChild(chip);

  var lastEvent = Date.now();
  function say(text, cls) { label.textContent = text; chip.className = "live-chip" + (cls ? " " + cls : ""); }
  say("live · " + REVISION.slice(11, 19) + "Z");

  var source;
  try { source = new EventSource("/v1/events"); } catch (e) { say("live updates unavailable", "down"); return; }
  source.onmessage = function (event) {
    lastEvent = Date.now();
    var data;
    try { data = JSON.parse(event.data); } catch (e) { return; }
    if (data.status === "error") { say("collector: " + (data.detail || "error"), "down"); return; }
    if (data.revision && data.revision !== REVISION) { remember(); location.reload(); return; }
    var stamp = String(data.revision || REVISION).slice(11, 19) + "Z";
    // A render that failed its checks is still served, but must not read as live.
    if (data.status === "degraded") { say("checks failed · " + stamp, "down"); return; }
    say("live · " + stamp);
  };
  source.onerror = function () { say("reconnecting…", "stale"); };
  setInterval(function () {
    // Heartbeats arrive well inside this window; silence means the service died.
    if (Date.now() - lastEvent > 90000) say("service not responding", "down");
  }, 15000);
})();
</script>
"""


def _wrap(body: str, extra_head: str = "", extra_body: str = "") -> str:
    return HEAD + extra_head + "</head><body>" + body + extra_body + TAIL


def compose(template_html: str, snapshot: dict, *, kids: dict[int, list[dict]],
            moment: datetime | None = None, delta_from: str | None = None) -> tuple[str, template.Report]:
    """Inject one snapshot into the template and return the body plus a report."""
    moment = moment or server_moment(snapshot)
    snapshot_date = moment.strftime("%Y-%m-%d")
    refreshed = moment.strftime("%H:%M UTC")
    if not delta_from:
        delta_from = _delta_window(template_html, snapshot_date)
    return template.inject(
        template_html,
        snapshot,
        snapshot_date=snapshot_date,
        delta_from=delta_from,
        refreshed=refreshed,
        kids=kids,
    )


def _var(html: str, pattern) -> str | None:
    match = pattern.search(html)
    return match.group(0).split('"')[1] if match else None


def _delta_window(template_html: str, snapshot_date: str) -> str:
    """The "changed since" label.

    It only moves when the calendar day rolls over: several refreshes a day all
    describe movement since the same starting point, and resetting the window on
    every poll would make the delta panel claim a shorter history than it shows.
    """
    previous = _var(template_html, template.VAR_SNAPSHOT)
    if previous and previous != snapshot_date:
        return previous[5:]
    return _var(template_html, template.VAR_DELTA) or snapshot_date[5:]


def server_moment(snapshot: dict) -> datetime:
    """The ADO server clock from the pull, falling back to this machine's.

    Accepts both a bare RFC 2822 date and the whole `Date: ...` header line,
    because the PowerShell pipeline this replaced stored the latter.
    """
    raw = (snapshot or {}).get("serverDate")
    if raw:
        if raw.lower().startswith("date:"):
            raw = raw.split(":", 1)[1].strip()
        try:
            parsed = parsedate_to_datetime(raw)
        except (ValueError, TypeError):
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
    return now_utc()


# A render that failed its checks is still served, so a reader can see what the
# board said; this banner says not to trust it. The colours are the board's own
# tokens, so it follows the light and dark themes.
ALERT_STYLE = """
<style>
  .render-alert {
    padding: 10px 20px; font: 13px/1.45 "Segoe UI", system-ui, sans-serif;
    background: var(--danger-soft, #f7e3e1); color: var(--ink, #1c2530);
    border-bottom: 2px solid var(--danger, #b3382e);
  }
  .render-alert b { color: var(--danger, #b3382e); }
</style>
"""


def _alert(degraded: str | None) -> tuple[str, str]:
    """Style and banner for a render that failed its checks; empty when it passed."""
    if not degraded:
        return "", ""
    banner = ('<div class="render-alert" role="alert"><b>This page failed its own checks.</b> '
              "Treat what it says as unverified until the next good refresh: "
              f"{template.escape(degraded)}</div>")
    return ALERT_STYLE, banner


def live_page(body: str, revision: str, degraded: str | None = None) -> str:
    style, banner = _alert(degraded)
    return _wrap(banner + body, LIVE_STYLE + style, LIVE_SCRIPT.replace("__REVISION__", revision))


def static_page(body: str, refresh_s: int = STATIC_REFRESH, degraded: str | None = None) -> str:
    meta = f'<meta http-equiv="refresh" content="{refresh_s}">'
    style, banner = _alert(degraded)
    return _wrap(banner + body, meta + style)


def artifact_page(body: str) -> str:
    """Body only. Publishing wraps it, and the Artifact tool wants the fragment."""
    return body


def revision_of(snapshot: dict) -> str:
    return iso(server_moment(snapshot))
