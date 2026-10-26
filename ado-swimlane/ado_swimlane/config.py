"""Paths and constants shared by every module."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_HOME = "ADO_SWIMLANE_HOME"
SERVICE_TASK_NAME = "AdoSwimlaneService"
WATCHDOG_TASK_NAME = "AdoSwimlaneWatchdog"
DEFAULT_PORT = 8799

# Azure DevOps. The resource GUID is the fixed first-party application id for
# Azure DevOps; `az account get-access-token --resource <it>` mints a token from
# the CLI's existing sign-in, so nothing here stores or needs a PAT.
ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"
DEFAULT_ORG = "https://dev.azure.com/rdprokes"
DEFAULT_PROJECT = "AdaptiveAssetAllocation"

# Poll cadences, in seconds. The board moves on a human/agent timescale (minutes
# at best), so polling faster than this only burns API calls and az sign-in use.
COLLECT_INTERVAL = 300
SUPERVISE_INTERVAL = 60
# Static pages have no push channel; they reload themselves on this cadence.
STATIC_REFRESH = 300

# --- Publishing -------------------------------------------------------------
# Polling and rendering are free; publishing is not. A hosted Artifact cannot
# reach this machine or Azure DevOps, so the page can never fetch its own data
# and every republish costs one Claude invocation. Everything below exists to
# make sure those invocations are spent only on a page that would actually look
# different.
ARTIFACT_URL = "https://claude.ai/code/artifact/6e7b521d-8499-4e66-920f-967f30daac8e"
# The artifact's emoji, fixed for its lifetime: readers recognise the page by it,
# and a changed one reads as a different artifact. Written as an escape so the
# value survives a Windows console codepage on its way through subprocess.
ARTIFACT_FAVICON = "\U0001F3CA"  # swimmer
# Coalesce a burst of edits rather than publishing each one.
PUBLISH_MIN_INTERVAL = 600
# A bulk sweep - AB#3526 touched 302 items in 18 minutes - must not be able to
# turn into a day's worth of sessions.
PUBLISH_DAILY_CAP = 24
PUBLISH_MODEL = "haiku"
PUBLISH_MAX_TURNS = 8
PUBLISH_TIMEOUT = 600


def artifact_url() -> str:
    return os.environ.get("ADO_SWIMLANE_ARTIFACT") or ARTIFACT_URL

# States that mean the item is off the board.
DONE_STATES = frozenset({"Closed", "Removed", "Done"})
# States that mean the work was delivered. Removed is off the board too, but
# nothing was delivered, so it never counts as a closure.
CLOSED_STATES = frozenset({"Closed", "Done"})


def is_open(state: str | None) -> bool:
    return state not in DONE_STATES


def org() -> str:
    return os.environ.get("ADO_SWIMLANE_ORG") or DEFAULT_ORG


def project() -> str:
    return os.environ.get("ADO_SWIMLANE_PROJECT") or DEFAULT_PROJECT


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def snapshot(self) -> Path:
        """Newest live board pull."""
        return self.home / "snapshot.json"

    @property
    def previous(self) -> Path:
        """The pull before it, kept so a refresh can diff without a database."""
        return self.home / "snapshot.prev.json"

    @property
    def states(self) -> Path:
        """Last state seen for every work item, across all polls.

        A pull only fetches what changed since the previous one, so comparing
        consecutive snapshots misses any item that moves exactly once. This is
        the memory that makes a single transition visible.
        """
        return self.home / "states.json"

    @property
    def changes(self) -> Path:
        """Append-only log of per-refresh change reports."""
        return self.home / "changes.jsonl"

    @property
    def html(self) -> Path:
        """Rendered static page (meta-refresh, no server behind it)."""
        return self.home / "swimlane.html"

    @property
    def artifact(self) -> Path:
        """Rendered page with no live-reload wiring, for publishing as an Artifact."""
        return self.home / "swimlane.artifact.html"

    @property
    def publisher(self) -> Path:
        """What was last published, when, and the session that published it."""
        return self.home / "publisher.json"

    @property
    def service_info(self) -> Path:
        return self.home / "service.json"

    @property
    def log(self) -> Path:
        return self.home / "service.log"


def default_home() -> Path:
    env = os.environ.get(ENV_HOME)
    return Path(env).expanduser() if env else Path.home() / ".ado-swimlane"


def get_paths(home: Path | str | None = None) -> Paths:
    paths = Paths(Path(home).expanduser() if home else default_home())
    paths.home.mkdir(parents=True, exist_ok=True)
    return paths


def project_root() -> Path:
    """Directory holding the launcher script; doubles as the Claude plugin root."""
    return Path(__file__).resolve().parent.parent


def template_path() -> Path:
    """The curated board. Prose lives here and is never machine-written."""
    env = os.environ.get("ADO_SWIMLANE_TEMPLATE")
    return Path(env).expanduser() if env else project_root() / "board" / "board.html"
