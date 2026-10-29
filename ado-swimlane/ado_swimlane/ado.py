"""Azure DevOps reads using an inherited PAT; no credentials are persisted."""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .config import is_open, org, project

FIELDS = [
    "System.Id",
    "System.WorkItemType",
    "System.State",
    "System.Title",
    "System.ChangedDate",
    "System.Parent",
    "System.Tags",
    "System.IterationPath",
]

# workitemsbatch caps at 200 ids; 190 leaves room without a second round trip.
BATCH_SIZE = 190
NOT_DONE = "[System.State] NOT IN ('Closed','Removed','Done')"


class AdoError(RuntimeError):
    """Any failure that should mark the collector unhealthy without killing it."""


class AuthError(AdoError):
    """The collector has no usable PAT or the provider rejected it."""


@dataclass
class Client:
    """One ADO session. Optional token injection is an in-memory PAT for tests."""

    token: str | None = field(default=None, repr=False)
    expiry: float = 0.0
    server_date: str | None = None
    _urlopen: object = field(default=None, repr=False)

    def _auth(self) -> str:
        # Read the inherited PAT each time so rotation needs no cached credential.
        # Never fall back to Entra/az account: the dedicated CLI config is PAT-only.
        pat = os.environ.get("AZURE_DEVOPS_EXT_PAT") or self.token
        if not pat or not pat.strip():
            raise AuthError(
                "AZURE_DEVOPS_EXT_PAT is missing; start the collector from the "
                "authorized PAT environment (Azure CLI config: "
                "C:/Users/rdpro/.codex/azure-cli-pat). Entra fallback is disabled."
            )
        return "Basic " + base64.b64encode((":" + pat).encode("utf-8")).decode("ascii")

    def _open(self, request):
        opener = self._urlopen or urllib.request.urlopen
        return opener(request, timeout=60)

    def post(self, url: str, body: dict, attempts: int = 4) -> dict:
        data = json.dumps(body).encode("utf-8")
        last: Exception | None = None
        for attempt in range(attempts):
            request = urllib.request.Request(url, data=data, method="POST")
            request.add_header("Authorization", self._auth())
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
                    raise AuthError(
                        f"ADO HTTP {exc.code}: PAT rejected or missing required read access; "
                        "restore the authorized PAT environment."
                    ) from None
                if exc.code not in (429, 500, 502, 503, 504):
                    # Provider bodies/URLs can echo credentials; keep errors bounded
                    # to status codes, including tracebacks written by the service.
                    raise AdoError(f"ADO HTTP {exc.code}") from None
            except (OSError, ValueError) as exc:
                last = exc
            else:
                if payload.get("message") and not payload.get("value") and not payload.get("workItems"):
                    raise AdoError("ADO returned an error response")
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
            payload = self.post(url, {"ids": chunk, "fields": FIELDS, "errorPolicy": "omit"})
            out.extend(payload.get("value", []))
        return out


def item_row(raw: dict) -> dict:
    """Flatten one workitemsbatch entry to the shape the board and diff use."""
    fields = raw.get("fields", {})
    parent = fields.get("System.Parent")
    return {
        "id": int(raw["id"]),
        "type": fields.get("System.WorkItemType"),
        "state": fields.get("System.State"),
        "title": fields.get("System.Title") or "",
        "parent": int(parent) if parent else None,
        "changed": fields.get("System.ChangedDate"),
        "tags": fields.get("System.Tags") or "",
        "iter": fields.get("System.IterationPath") or "",
    }


def collect(card_ids: list[int], known_ids: list[int], *, since: str, client: Client | None = None) -> dict:
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

    union = sorted(set(card_ids) | set(known_ids) | set(open_ef) | set(open_bugs) | set(changed) | set(children))
    items = [item_row(raw) for raw in client.batch(union)]

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
