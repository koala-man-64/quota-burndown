"""Azure DevOps access: token, WIQL, and batched work-item detail.

Auth deliberately borrows the Azure CLI's existing sign-in rather than storing a
PAT. Nothing secret is written to disk by this module, and nothing secret can
reach the rendered page, because the page is built from work-item fields only.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .config import ADO_RESOURCE, is_open, org, project

PRIORITY_FIELD = "Microsoft.VSTS.Common.Priority"

# workitemsbatch caps at 200 ids; 190 leaves room without a second round trip.
BATCH_SIZE = 190
NOT_DONE = "[System.State] NOT IN ('Closed','Removed','Done')"


class AdoError(RuntimeError):
    """Any failure that should mark the collector unhealthy without killing it."""


class AuthError(AdoError):
    """The Azure CLI has no usable session; a human has to run `az login`."""


def _az_executable() -> str:
    # On Windows `az` is a .cmd shim, which subprocess will not find without the
    # extension; shutil.which resolves it using PATHEXT.
    found = shutil.which("az")
    if not found:
        raise AuthError("azure-cli not found on PATH; install it or run `az login`")
    return found


def fetch_token() -> tuple[str, float]:
    """Return (token, expiry_epoch_seconds) from the Azure CLI's cached login."""
    try:
        result = subprocess.run(
            [_az_executable(), "account", "get-access-token", "--resource", ADO_RESOURCE, "-o", "json"],
            capture_output=True,
            text=True,
            timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuthError(f"az token fetch failed: {type(exc).__name__}") from exc
    if result.returncode:
        raise AuthError(f"az token fetch failed ({result.returncode}): {(result.stderr or result.stdout).strip()[:200]}")
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise AuthError("az returned non-JSON token output") from exc
    token = payload.get("accessToken")
    if not token:
        raise AuthError("az returned no accessToken")
    # expires_on is epoch seconds; expiresOn is a local-time string. Prefer the
    # unambiguous one and fall back to a conservative short lifetime.
    try:
        expiry = float(payload.get("expires_on"))
    except (TypeError, ValueError):
        expiry = time.time() + 600
    return token, expiry


@dataclass
class Client:
    """One ADO session. Holds the access token and the server clock."""

    token: str | None = None
    expiry: float = 0.0
    server_date: str | None = None
    _urlopen: object = field(default=None, repr=False)

    def _auth(self) -> str:
        # Refresh a minute early so a long batch never straddles the expiry.
        if not self.token or time.time() >= self.expiry - 60:
            self.token, self.expiry = fetch_token()
        return self.token

    def _open(self, request):
        opener = self._urlopen or urllib.request.urlopen
        return opener(request, timeout=60)

    def post(self, url: str, body: dict, attempts: int = 4) -> dict:
        data = json.dumps(body).encode("utf-8")
        last: Exception | None = None
        for attempt in range(attempts):
            request = urllib.request.Request(url, data=data, method="POST")
            request.add_header("Authorization", "Bearer " + self._auth())
            # rdprokes is an MSA-backed organization: without this header the
            # REST API answers 203 with a sign-in page instead of JSON.
            request.add_header("X-VSS-ForceMsaPassThrough", "true")
            request.add_header("Content-Type", "application/json")
            try:
                with self._open(request) as response:
                    self.server_date = response.headers.get("Date") or self.server_date
                    payload = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code in (401, 403):
                    # Force a new credential once, then give up rather than spin.
                    self.token, self.expiry = None, 0.0
                elif exc.code not in (429, 500, 502, 503, 504):
                    # ADO puts the actual complaint in the body; without it a 400
                    # says nothing about which clause it rejected.
                    try:
                        detail = json.loads(exc.read().decode("utf-8")).get("message", "")
                    except (ValueError, OSError):
                        detail = ""
                    raise AdoError(f"ADO HTTP {exc.code} for {url}: {detail[:300]}") from exc
            except (OSError, ValueError) as exc:
                last = exc
            else:
                if payload.get("message") and not payload.get("value") and not payload.get("workItems"):
                    raise AdoError("ADO error: " + str(payload["message"])[:200])
                return payload
            time.sleep(2 ** attempt)
        raise AdoError(f"ADO request failed after {attempts} attempts: {type(last).__name__}")

    def wiql(self, query: str) -> list[int]:
        # timePrecision is a query-string parameter, not a body field. Without it
        # WIQL runs in date precision and rejects any bound carrying a time other
        # than midnight - exactly what an "everything since the last pull" window
        # needs.
        url = f"{org()}/{project()}/_apis/wit/wiql?timePrecision=true&api-version=7.1"
        payload = self.post(url, {"query": query})
        return [int(item["id"]) for item in payload.get("workItems", [])]

    def batch(self, ids: list[int]) -> list[dict]:
        url = f"{org()}/_apis/wit/workitemsbatch?api-version=7.1"
        out: list[dict] = []
        for start in range(0, len(ids), BATCH_SIZE):
            chunk = ids[start:start + BATCH_SIZE]
            payload = self.post(url, {
                "ids": chunk,
                # ADO rejects fields together with $expand. Normalize only the
                # needed fields from the provider's expanded default response.
                "$expand": "Relations",
                "errorPolicy": "omit",
            })
            out.extend(payload.get("value", []))
        return out


def item_row(raw: dict) -> dict:
    """Flatten one workitemsbatch entry to the shape the board and diff use."""
    fields = raw.get("fields", {})
    parent = fields.get("System.Parent")
    try:
        revision = int(raw["rev"]) if raw.get("rev") is not None else None
    except (KeyError, TypeError, ValueError):
        revision = None
    try:
        parent = int(parent) if parent else None
    except (TypeError, ValueError):
        parent = None
    assigned = fields.get("System.AssignedTo")
    if isinstance(assigned, dict):
        assigned = assigned.get("displayName") or assigned.get("uniqueName")
    priority = fields.get(PRIORITY_FIELD)
    try:
        priority = int(priority) if priority is not None else None
    except (TypeError, ValueError):
        priority = None
    return {
        "id": int(raw["id"]),
        "revision": revision,
        "type": fields.get("System.WorkItemType"),
        "state": fields.get("System.State"),
        "title": fields.get("System.Title") or "",
        "parent": parent,
        "changed": fields.get("System.ChangedDate"),
        "tags": fields.get("System.Tags") or "",
        "iter": fields.get("System.IterationPath") or "",
        "priority": priority,
        "priorityReturned": PRIORITY_FIELD in fields,
        "assignedTo": str(assigned).strip() if assigned else None,
        "assignedToReturned": "System.AssignedTo" in fields,
        "iterationReturned": "System.IterationPath" in fields,
        "relations": _dependency_relations(int(raw["id"]), raw.get("relations") or []),
        "relationsFetched": "relations" in raw,
    }


_WORK_ITEM_URL_ID = re.compile(r"(?:^|/)workitems/(\d+)(?:[/?#]|$)", re.IGNORECASE)
_DEPENDENCY_FORWARD = "System.LinkTypes.Dependency-Forward"
_DEPENDENCY_REVERSE = "System.LinkTypes.Dependency-Reverse"


def _dependency_relations(item_id: int, relations: list[dict]) -> list[dict]:
    """Normalize ADO predecessor/successor links to predecessor -> successor."""
    edges: set[tuple[int, int]] = set()
    for relation in relations:
        if not isinstance(relation, dict):
            continue
        kind = relation.get("rel")
        if kind not in (_DEPENDENCY_FORWARD, _DEPENDENCY_REVERSE):
            continue
        match = _WORK_ITEM_URL_ID.search(str(relation.get("url") or ""))
        if not match:
            continue
        linked_id = int(match.group(1))
        edge = (item_id, linked_id) if kind == _DEPENDENCY_FORWARD else (linked_id, item_id)
        if edge[0] != edge[1]:
            edges.add(edge)
    return [
        {"predecessor": before, "successor": after}
        for before, after in sorted(edges)
    ]


def collect(card_ids: list[int], known_ids: list[int], *, since: str,
            tracked_items: list[dict] | None = None,
            client: Client | None = None) -> dict:
    """Pull one complete board snapshot.

    `since` bounds the "what moved" query; `known_ids` are the ids the template
    already references, so a card never loses its detail just because nothing
    touched it this window.
    """
    client = client or Client()
    proj = project()
    scope = f"[System.TeamProject] = '{proj}'"

    open_ef = client.wiql(
        f"SELECT [System.Id] FROM WorkItems WHERE {scope} "
        f"AND [System.WorkItemType] IN ('Epic','Feature') AND {NOT_DONE}"
    )
    open_bugs = client.wiql(
        f"SELECT [System.Id] FROM WorkItems WHERE {scope} AND [System.WorkItemType] = 'Bug' AND {NOT_DONE}"
    )
    changed = client.wiql(
        f"SELECT [System.Id] FROM WorkItems WHERE {scope} AND [System.ChangedDate] >= '{since}'"
    )
    children: list[int] = []
    if card_ids:
        in_list = ",".join(str(i) for i in card_ids)
        children = client.wiql(
            f"SELECT [System.Id] FROM WorkItems WHERE {scope} "
            f"AND [System.Parent] IN ({in_list}) AND {NOT_DONE}"
        )

    tracked_items = tracked_items or []
    tracked_ids = {int(item["id"]) for item in tracked_items}
    union = sorted(set(card_ids) | set(known_ids) | tracked_ids | set(open_ef) | set(open_bugs) | set(changed) | set(children))
    raw_items = client.batch(union)
    items = [item_row(raw) for raw in raw_items]
    requested = set(union)
    returned = {item["id"] for item in items}
    card_set = set(card_ids)
    relation_returned = {
        int(raw["id"]) for raw in raw_items
        if int(raw["id"]) in card_set and "relations" in raw
    }
    dependency_sources: dict[tuple[int, int], set[int]] = {}
    for item in items:
        source_id = int(item["id"])
        # Relations are read only from the displayed card records already in
        # this bounded batch. Keep linked endpoint IDs, but never crawl them.
        if source_id not in card_set or not item.get("relationsFetched"):
            continue
        for edge in item.get("relations", []):
            key = (int(edge["predecessor"]), int(edge["successor"]))
            dependency_sources.setdefault(key, set()).add(source_id)
    dependencies = sorted(dependency_sources.items())

    return {
        # The ADO server's own clock, not this machine's: every timestamp the
        # board shows has to come from the same authority as the data.
        "serverDate": client.server_date,
        "since": since,
        "cardIds": card_ids,
        "openEF": open_ef,
        "openBugs": open_bugs,
        "changed": changed,
        "children": children,
        # The query bounds only changed-item reporting. Item details cover cards,
        # known child ids, open project work, changed ids, and direct open
        # children of current cards; it is not a project-wide detail census.
        "queryScope": {
            "project": proj,
            "changedSince": since,
            "details": "selected work-item IDs, not a project-wide detail census",
            "detailIdCount": len(requested),
            "detailReturnedCount": len(returned),
            "detailMissingIds": sorted(requested - returned),
            "cardIds": sorted(card_set),
            "trackedIds": sorted(tracked_ids),
            "trackedReturnedIds": sorted(tracked_ids & returned),
            "trackedMissingIds": sorted(tracked_ids - returned),
            "relationExpand": "Relations",
            "relationEndpointDetailPolicy": (
                "off-view endpoints are not fetched solely because a displayed card links to them"
            ),
            "cardRelationReturnedIds": sorted(relation_returned),
            "cardRelationMissingIds": sorted(card_set - relation_returned),
        },
        "trackedItems": tracked_items,
        "dependencies": [
            {"predecessor": before, "successor": after, "observedBy": sorted(sources)}
            for (before, after), sources in dependencies
        ],
        "items": items,
    }


def open_children_of(snapshot: dict) -> dict[int, list[dict]]:
    """Open children per card id, sorted, ready for the CHILDREN map."""
    cards = set(snapshot.get("cardIds", []))
    out: dict[int, list[dict]] = {}
    for item in snapshot.get("items", []):
        parent = item.get("parent")
        if parent in cards and is_open(item.get("state")):
            out.setdefault(parent, []).append(item)
    for kids in out.values():
        kids.sort(key=lambda i: i["id"])
    return out
