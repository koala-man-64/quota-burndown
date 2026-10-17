"""Republish the board as a Claude Artifact, only when that is worth a session.

A hosted Artifact cannot poll Azure DevOps: it has no route to this machine, and
it must never carry a credential. So unlike the loopback page - which the
service updates for free - every Artifact update costs one Claude invocation,
because only a Claude session holds the publishing tool.

Two things follow, and they are the whole design of this module:

  * publish only when the page would actually differ, decided by fingerprint
    rather than by guessing which changes are "interesting";
  * spend the fewest sessions that keeps it current, via a debounce, a daily
    cap, and one pinned session reused across publishes.

That last one is not a micro-optimisation. The Artifact tool refuses to
republish to a URL the *conversation* has not read, and reading this board costs
115 KB of context. Pinning a session id means that is paid once, not per
publish.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import date

from . import template
from .config import (
    PUBLISH_DAILY_CAP,
    PUBLISH_MAX_TURNS,
    PUBLISH_MIN_INTERVAL,
    PUBLISH_MODEL,
    PUBLISH_TIMEOUT,
    ARTIFACT_FAVICON,
    Paths,
    artifact_url,
)
from .util import iso, now_utc, read_json, write_json

# Volatile by construction: these change on every single poll, so including them
# in the fingerprint would make every refresh look like a change and defeat the
# entire gate.
VOLATILE = (template.VAR_SNAPSHOT, template.VAR_DELTA, template.VAR_REFRESHED)
VERSION_RE = re.compile(r"[Vv]ersion\s+(\d+)")


def fingerprint(body: str) -> str:
    """Identity of the page's *content*, ignoring the refresh timestamps.

    Exact rather than heuristic: if this changes, something a reader can see
    changed - a card state, the children map, a count, or prose someone edited
    in the template. If it does not change, a republish would produce a visually
    identical page and is not worth a session.
    """
    normalised = body
    for pattern in VOLATILE:
        normalised = pattern.sub(lambda m: m.group(1) + m.group(2), normalised)
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


@dataclass
class PublishState:
    fingerprint: str | None = None
    published_at: str | None = None
    day: str | None = None
    count_today: int = 0
    session_id: str | None = None
    # True once the pinned session has published this artifact at least once.
    # Until then its publishes are refused, because the headless Artifact tool
    # has no "read" action and so cannot establish a baseline any other way.
    bootstrapped: bool = False
    version: str | None = None
    last_error: str | None = None

    @classmethod
    def load(cls, paths: Paths) -> "PublishState":
        return cls(**{k: v for k, v in (read_json(paths.publisher, {}) or {}).items()
                      if k in cls.__dataclass_fields__})

    def save(self, paths: Paths) -> None:
        write_json(paths.publisher, self.__dict__)

    def roll_day(self, today: str) -> None:
        if self.day != today:
            self.day, self.count_today = today, 0


@dataclass
class Decision:
    publish: bool
    reason: str
    fingerprint: str = ""
    retry_after: int = 0


def decide(state: PublishState, body: str, *, now=None, min_interval: int = PUBLISH_MIN_INTERVAL,
           daily_cap: int = PUBLISH_DAILY_CAP, force: bool = False) -> Decision:
    now = now or now_utc()
    current = fingerprint(body)
    state.roll_day(now.date().isoformat())

    if force:
        return Decision(True, "forced", current)
    if state.fingerprint == current:
        return Decision(False, "unchanged: the published page would look identical", current)
    if state.count_today >= daily_cap:
        return Decision(False, f"daily cap reached ({daily_cap})", current)
    if state.published_at:
        try:
            from datetime import datetime

            last = datetime.fromisoformat(state.published_at.replace("Z", "+00:00"))
            elapsed = (now - last).total_seconds()
        except ValueError:
            elapsed = min_interval
        if elapsed < min_interval:
            # Coalesce: the change is real, it just has not settled yet.
            return Decision(False, f"debounced ({int(min_interval - elapsed)}s to go)", current,
                            retry_after=int(min_interval - elapsed))
    return Decision(True, "content changed", current)


def _claude() -> str:
    found = shutil.which("claude")
    if not found:
        raise RuntimeError("claude CLI not found on PATH; cannot publish")
    return found


BOOTSTRAP_PROMPT = """Publish an already-prepared local file to an existing Claude Artifact, updating it in place.

Call the Artifact tool once with: file_path "{path}", url "{url}", label "{label}", favicon "{favicon}", force true.

force:true is correct here and the user has explicitly authorised this one publish. This session cannot read the artifact first (the Artifact tool available here has no "read" action), and the local file is a generated render of the source of truth, so there is nothing in the live version to preserve. The favicon is the artifact's existing one and must be passed exactly as given, unchanged. Do not read, write or edit any file.

Reply with just the published version number, or REFUSED plus the reason."""

RESUME_PROMPT = """Publish the local file {path} to the same artifact as before ({url}), with label "{label}" and favicon "{favicon}". Pass the url parameter so it updates in place, and the favicon exactly as given.

Do NOT pass force. Do not read, write or edit any file. If the publish is refused because the live version is newer, that is the correct outcome: reply exactly REFUSED plus the reason and stop.

Reply with just the published version number, or REFUSED."""


# The Claude CLI only exposes the Artifact tool when it believes the desktop app
# is hosting it, and it decides that from this one variable. A `claude -p` run
# from inside the desktop app inherits it; the service, started at logon with no
# desktop app parent, does not - so every publish came back "Artifact tool is
# disabled for this session" while the same command worked from a chat.
# It is a label, not a credential: auth still comes from the CLI's own sign-in.
# Undocumented, so a CLI update can move it; the refusal text in `status` is
# where that would show up first.
DESKTOP_ENTRYPOINT = ("CLAUDE_CODE_ENTRYPOINT", "claude-desktop")


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault(*DESKTOP_ENTRYPOINT)
    return env


def _run(args: list[str], prompt: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        args + ["-p", prompt],
        capture_output=True,
        text=True,
        timeout=PUBLISH_TIMEOUT,
        env=_env(),
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )


# Settings-validation warnings are printed on every run and would otherwise be
# the only thing visible in a truncated error, hiding the actual cause.
NOISE = ("permission allow rule", "use edit(")
AUTH_HINT = "the claude CLI sign-in has expired or been revoked; run `claude /login`, then `publish --force`"


def _failure(result, output: str) -> str:
    """The most useful line from a failed run.

    The CLI reports authentication failures on stdout, not stderr, so the
    obvious `stderr or stdout` ordering surfaces boilerplate warnings and hides
    "OAuth access token has been revoked" - which is exactly the failure a
    person has to act on.
    """
    lines = [line.strip() for line in ((output or "") + "\n" + (result.stderr or "")).splitlines()
             if line.strip() and not any(n in line.lower() for n in NOISE)]
    text = " ".join(lines)[:300]
    if "authenticate" in text.lower() or "401" in text or "access token" in text.lower():
        return f"{AUTH_HINT} — {text}"
    return f"claude exited {result.returncode}: {text}" if text else f"claude exited {result.returncode}"


def publish(paths: Paths, *, label: str, url: str | None = None, state: PublishState | None = None,
            model: str = PUBLISH_MODEL, _retried: bool = False) -> tuple[bool, str]:
    """Hand the rendered file to a Claude session and have it republish.

    The session id is pinned so that the first publish establishes a baseline
    the later ones inherit. That first one has to be forced, because a headless
    session has no way to read the live version; every publish after it is an
    ordinary republish that will refuse rather than overwrite a newer version.
    """
    url = url or artifact_url()
    state = state or PublishState.load(paths)
    claude = _claude()
    base = [claude, "--model", model, "--max-turns", str(PUBLISH_MAX_TURNS),
            "--allowed-tools", "Artifact"]
    path = str(paths.artifact)

    if state.session_id and state.bootstrapped:
        args = base + ["--resume", state.session_id]
        prompt = RESUME_PROMPT.format(path=path, url=url, label=label, favicon=ARTIFACT_FAVICON)
    else:
        # A session id that has never been used must be introduced with
        # --session-id; --resume on a fresh id fails with "No conversation
        # found", which is how a lost session turned into a permanent jam.
        state.session_id = state.session_id or str(uuid.uuid4())
        args = base + ["--session-id", state.session_id]
        prompt = BOOTSTRAP_PROMPT.format(path=path, url=url, label=label, favicon=ARTIFACT_FAVICON)

    try:
        result = _run(args, prompt)
    except subprocess.TimeoutExpired:
        return False, f"publish timed out after {PUBLISH_TIMEOUT}s"

    output = (result.stdout or "").strip()
    if result.returncode and not _retried:
        # A pinned session ages out eventually ("No conversation found"). Taking
        # a fresh one costs a bootstrap force, so only do it when the last
        # publish of ours succeeded - then the live version is the one this
        # pipeline put there, and forcing over it discards nothing. After a
        # refusal (someone else published) that is not true, so it stays jammed
        # for a person to clear, which is the agreed behaviour.
        if state.fingerprint and not (state.last_error or "").startswith("REFUSED"):
            state.session_id, state.bootstrapped = str(uuid.uuid4()), False
            return publish(paths, label=label, url=url, state=state, model=model, _retried=True)
        return False, ("session lost and the last publish did not succeed; "
                       "re-establish the baseline with `publish --force`")
    if result.returncode:
        return False, _failure(result, output)
    if "REFUSED" in output:
        return False, output[:300]

    match = VERSION_RE.search(output)
    state.version = match.group(1) if match else None
    state.bootstrapped = True
    return True, output[:200]


def run_if_due(paths: Paths, body: str, *, label: str, force: bool = False,
               url: str | None = None, dry_run: bool = False, now=None,
               min_interval: int = PUBLISH_MIN_INTERVAL,
               daily_cap: int = PUBLISH_DAILY_CAP) -> tuple[bool, str]:
    """The gate plus the publish. Used by the service and by `publish` on the CLI."""
    state = PublishState.load(paths)
    decision = decide(state, body, now=now, min_interval=min_interval, daily_cap=daily_cap, force=force)
    if not decision.publish:
        return False, decision.reason
    if dry_run:
        return False, f"would publish ({decision.reason})"

    if force:
        # An explicit force is a re-bootstrap: the running session's baseline is
        # the thing that went stale, so it needs a new one.
        state.session_id, state.bootstrapped = None, False
    ok, detail = publish(paths, label=label, url=url, state=state)
    now = now_utc()
    if ok:
        state.fingerprint = decision.fingerprint
        state.published_at = iso(now)
        state.roll_day(now.date().isoformat())
        state.count_today += 1
        state.last_error = None
    else:
        state.last_error = detail
    state.save(paths)
    return ok, detail


def label_for(revision: str) -> str:
    """Artifact labels are capped at 60 characters."""
    return f"auto refresh {revision[5:16].replace('T', ' ')} UTC"[:60]
