"""Offline protocol and full-board replays; never historical full-response claims."""
import copy
import http.client
import io
import json
from pathlib import Path

import pytest

from ado_swimlane import ado, diff, refresh, render, template, validate
from ado_swimlane.config import get_paths


FIXTURES = Path(__file__).parent / "fixtures"
BOARD = Path(__file__).resolve().parent.parent / "board" / "board.html"
FORWARD = "System.LinkTypes.Dependency-Forward"
REVERSE = "System.LinkTypes.Dependency-Reverse"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def response(payload, *, framing="length", continuation=False, status=200, extra_headers=()):
    """Use the real HTTP parser, including terminal-chunk verification."""
    body = json.dumps(payload).encode()
    headers = ["Date: Thu, 29 Oct 2026 15:10:51 GMT", "Content-Type: application/json"]
    if framing == "length":
        headers.append(f"Content-Length: {len(body)}")
    elif framing == "bad-length":
        headers.append("Content-Length: invalid")
    elif framing == "short-body":
        headers.append(f"Content-Length: {len(body) + 1}")
    elif framing in ("chunked", "truncated-chunked"):
        headers.append("Transfer-Encoding: chunked")
        body = f"{len(body):x}\r\n".encode() + body + b"\r\n"
        if framing == "chunked":
            body += b"0\r\n\r\n"
    if continuation:
        headers.append("x-ms-continuationtoken: more")
    headers.extend(f"{name}: {value}" for name, value in extra_headers)
    wire = f"HTTP/1.1 {status} Result\r\n".encode() + "\r\n".join(headers).encode() + b"\r\n\r\n" + body

    class Socket:
        def makefile(self, *_args, **_kwargs):
            return io.BytesIO(wire)

    parsed = http.client.HTTPResponse(Socket())
    parsed.begin()
    return parsed


@pytest.fixture(autouse=True)
def offline_credentials(monkeypatch):
    monkeypatch.delenv("AZURE_DEVOPS_EXT_PAT", raising=False)
    monkeypatch.setattr(ado, "fetch_token", lambda: ("test-only", float("inf")))
    monkeypatch.setattr(ado.time, "sleep", lambda _seconds: None)


def test_observed_3384_projection_remains_unknown_without_link_query():
    payload = fixture("expanded-3384-omitted-relations.json")
    client = ado.Client(token="test-only", _urlopen=lambda *_a, **_k: response(payload))
    rows = client.batch([3384])
    assert "relations" not in rows[0]
    normalized = ado.item_row(rows[0])
    assert normalized["id"] == 3384 and normalized["state"] == "Closed"
    assert normalized["relationsFetched"] is False
    assert normalized["relations"] == []  # an empty representation is not evidence


@pytest.mark.parametrize("case", ["bad-count", "bool-count", "no-value", "duplicate", "foreign-id", "bad-fields"])
def test_invalid_expanded_batch_records_cannot_qualify_fallback(case):
    payload = fixture("expanded-3384-omitted-relations.json")
    if case == "bad-count": payload["count"] = 2
    elif case == "bool-count": payload["count"] = True
    elif case == "no-value": del payload["value"]
    elif case == "duplicate":
        payload["value"] *= 2
        payload["count"] = 2
    elif case == "foreign-id": payload["value"][0]["id"] = 999
    elif case == "bad-fields": payload["value"][0]["fields"] = None
    client = ado.Client(token="test-only", _urlopen=lambda *_a, **_k: response(payload))
    with pytest.raises(ado.AdoError):
        client.batch([3384])


@pytest.mark.parametrize("framing", ["length", "chunked"])
def test_complete_empty_mustcontain_query_qualifies_only_requested_source(framing):
    payload = fixture("3384-empty-dependency-links.json")
    calls = []

    def open_query(request, **_kwargs):
        calls.append((request.full_url, json.loads(request.data)))
        return response(payload, framing=framing)

    client = ado.Client(token="test-only", _urlopen=open_query)
    evidence = client.dependency_links([3384])
    assert evidence["dependencies"] == []
    assert set(evidence["observations"]) == {3384}
    assert evidence["observations"][3384] == {
        "method": "wiql-workitemlinks", "sourceId": 3384, "mode": "MustContain",
        "asOf": payload["asOf"], "complete": True, "rootMarkersIgnored": 0,
    }
    assert len(calls) == 1 and "$top" not in calls[0][0]
    assert "timePrecision=true" in calls[0][0]
    assert "[Source].[System.Id] = 3384" in calls[0][1]["query"]
    assert "MODE (MustContain)" in calls[0][1]["query"]


def test_directions_duplicates_offview_targets_and_known_root_markers():
    payload = fixture("3384-empty-dependency-links.json")
    payload["workItemRelations"] = [
        {"rel": None, "source": None, "target": {"id": 3384}},
        {"rel": FORWARD, "source": {"id": 3384}, "target": {"id": 77777}},
        {"rel": FORWARD, "source": {"id": 3384}, "target": {"id": 77777}},
        {"rel": REVERSE, "source": {"id": 3384}, "target": {"id": 2095}},
    ]
    client = ado.Client(token="test-only", _urlopen=lambda *_a, **_k: response(payload))
    evidence = client.dependency_links([3384])
    assert evidence["dependencies"] == [
        {"predecessor": 2095, "successor": 3384, "observedBy": [3384]},
        {"predecessor": 3384, "successor": 77777, "observedBy": [3384]},
    ]
    assert evidence["observations"][3384]["rootMarkersIgnored"] == 1


@pytest.mark.parametrize("framing,extra_headers", [
    ("length", [("x-ms-continuationtoken", ""), ("x-ms-continuationtoken", "more")]),
    ("length", [("x-ms-continuationtoken", ""), ("X-MS-ContinuationToken", "more")]),
    ("length", [("x-ms-continuationtoken", ""), ("x-ms-continuationtoken", "")]),
    ("length", [("Content-Length", "1")]),
    ("chunked", [("Transfer-Encoding", "gzip")]),
])
def test_duplicate_or_contradictory_headers_cannot_hide_continuation_or_framing(framing, extra_headers):
    payload = fixture("3384-empty-dependency-links.json")
    client = ado.Client(token="test-only", _urlopen=lambda *_a, **_k: response(
        payload, framing=framing, extra_headers=extra_headers))
    with pytest.raises(ado.AdoError):
        client.dependency_links([3384])


@pytest.mark.parametrize("case", [
    "wrong-type", "wrong-result", "no-rows", "null-rows", "no-asof", "naive-asof",
    "foreign-source", "foreign-root", "root-only", "bad-target", "wrong-link", "self-link",
    "ambiguous-workitems", "body-continuation", "partial", "header-continuation",
    "unknown-framing", "bad-length", "short-body", "truncated-chunked", "wrong-status",
])
def test_unknown_incomplete_or_ambiguous_link_responses_fail_closed(case):
    payload = fixture("3384-empty-dependency-links.json")
    kwargs = {}
    if case == "wrong-type": payload["queryType"] = "tree"
    elif case == "wrong-result": payload["queryResultType"] = "workItem"
    elif case == "no-rows": del payload["workItemRelations"]
    elif case == "null-rows": payload["workItemRelations"] = None
    elif case == "no-asof": del payload["asOf"]
    elif case == "naive-asof": payload["asOf"] = "2026-10-29T15:10:52"
    elif case in ("foreign-source", "bad-target", "wrong-link", "self-link"):
        payload["workItemRelations"] = [{
            "rel": "System.LinkTypes.Hierarchy-Forward" if case == "wrong-link" else FORWARD,
            "source": {"id": 999 if case == "foreign-source" else 3384},
            "target": {"id": None if case == "bad-target" else 3384 if case == "self-link" else 2095},
        }]
    elif case in ("foreign-root", "root-only"):
        payload["workItemRelations"] = [{"rel": None, "source": None,
                                         "target": {"id": 999 if case == "foreign-root" else 3384}}]
    elif case == "ambiguous-workitems": payload["workItems"] = []
    elif case == "body-continuation": payload["continuationToken"] = "more"
    elif case == "partial": payload["isPartial"] = True
    elif case == "header-continuation": kwargs["continuation"] = True
    elif case == "wrong-status": kwargs["status"] = 203
    else: kwargs["framing"] = case
    client = ado.Client(token="test-only", _urlopen=lambda *_a, **_k: response(payload, **kwargs))
    with pytest.raises(ado.AdoError):
        client.dependency_links([3384])


def synthetic_collection(*, literal_3384=False, missing_3384=False, bad_fallback=False,
                         relations_override="absent", extra_known=(), link_rows=None):
    """Synthetic full current-template dataset, not historical provider bytes."""
    board = BOARD.read_text(encoding="utf-8-sig")
    cards = {card.id: card for card in template.read_cards(board)}
    ids = set(template.known_ids(board)) | {2439, 4051} | set(extra_known)
    records = {}
    for wid in ids:
        state = cards[wid].effective_state if wid in cards else "New"
        if state in ("none", "Unknown"): state = "New"
        records[wid] = {"id": wid, "rev": 7, "fields": {
            "System.WorkItemType": "User Story" if wid == 4049 else "Feature",
            "System.State": state, "System.Title": f"Synthetic item {wid}",
            "System.ChangedDate": "2026-10-29T15:00:00Z", "System.IterationPath": "ExampleProject",
            "Microsoft.VSTS.Common.Priority": 2,
        }, "relations": []}
    for wid, parent in ((4049, 2439), (2439, 2095), (4051, 4049)):
        records[wid]["fields"]["System.Parent"] = parent
    records[2095]["relations"] = [{"rel": FORWARD,
        "url": "https://example.invalid/_apis/wit/workItems/2097"}]
    records[3384] = fixture("expanded-3384-omitted-relations.json")["value"][0]
    if literal_3384: records[3384]["relations"] = []
    if relations_override != "absent": records[3384]["relations"] = relations_override
    if missing_3384: del records[3384]
    calls = []

    def open_replay(request, **_kwargs):
        body = json.loads(request.data)
        calls.append((request.full_url, body))
        if "/workitemsbatch?" in request.full_url:
            assert body["$expand"] == "Relations" and "fields" not in body
            rows = [records[wid] for wid in body["ids"] if wid in records]
            return response({"count": len(rows), "value": rows})
        query = body["query"]
        if "FROM WorkItemLinks" in query:
            assert "[Source].[System.Id] = 3384" in query
            payload = fixture("3384-empty-dependency-links.json")
            if link_rows is not None: payload["workItemRelations"] = link_rows
            return response(payload, continuation=bad_fallback)
        if "[System.Parent] IN" in query:
            result = [wid for wid, raw in records.items()
                      if raw["fields"].get("System.Parent") in cards and ado.is_open(raw["fields"]["System.State"])]
        elif "[System.ChangedDate]" in query: result = []
        else:
            types = {"Bug"} if "[System.WorkItemType] = 'Bug'" in query else {"Epic", "Feature"}
            result = [wid for wid, raw in records.items() if raw["fields"]["System.WorkItemType"] in types
                      and ado.is_open(raw["fields"]["System.State"])]
        return response({"workItems": [{"id": wid} for wid in sorted(result)]})

    client = ado.Client(token="test-only", _urlopen=open_replay)
    snapshot = ado.collect(template.card_ids(board), sorted(set(template.known_ids(board)) | set(extra_known)),
                           tracked_items=template.tracked_items(board), since="2026-10-29T15:00:00Z", client=client)
    return board, snapshot, calls


def test_entire_synthetic_board_passes_only_with_complete_fallback_and_visible_provenance():
    board, snapshot, calls = synthetic_collection()
    body, report = render.compose(board, snapshot, kids=ado.open_children_of(snapshot))
    checks = validate.check(body, snapshot, expected_children=len(snapshot["children"]))
    assert checks.ok, checks.notes
    assert not report.missing
    assert set(snapshot["cardIds"]) == set(template.card_ids(board))
    assert snapshot["queryScope"]["cardRelationMissingIds"] == []
    assert 3384 not in snapshot["queryScope"]["cardRelationLiteralIds"]
    assert snapshot["queryScope"]["cardRelationFallback"]["sourceIds"] == [3384]
    row = next(item for item in snapshot["items"] if item["id"] == 3384)
    assert row["relationsFetched"] and row["relations"] == []
    assert row["relationEvidence"]["method"] == "wiql-workitemlinks"
    assert row["relationEvidence"]["asOf"] == "2026-10-29T15:10:52.08Z"
    assert "Explicit dependency queries covered 1 card." in body
    assert "Item details and links were read separately and may reflect different moments." in body
    assert sum("FROM WorkItemLinks" in body.get("query", "") for _url, body in calls) == 1
    assert all(len(body["ids"]) <= 190 for _url, body in calls if "ids" in body)
    literal_board, literal, literal_calls = synthetic_collection(literal_3384=True)
    assert literal_board == board
    assert not any("FROM WorkItemLinks" in body.get("query", "") for _url, body in literal_calls)
    changes = diff.compare(literal, snapshot)
    assert not (changes.dependencies_added or changes.dependencies_gone
                or changes.dependencies_scope_added or changes.dependencies_scope_changed)
    assert changes.is_quiet(), "evidence method alone is not a work-item or edge change"


@pytest.mark.parametrize("kwargs", [{"missing_3384": True}, {"bad_fallback": True},
                                    {"relations_override": None}, {"relations_override": {}},
                                    {"relations_override": [None]},
                                    {"relations_override": [{"rel": FORWARD, "url": "malformed"}]}])
def test_full_board_missing_records_incomplete_queries_and_malformed_literals_stay_unknown(kwargs):
    board, snapshot, calls = synthetic_collection(**kwargs)
    body, _ = render.compose(board, snapshot, kids=ado.open_children_of(snapshot))
    checks = validate.check(body, snapshot, expected_children=len(snapshot["children"]))
    assert not checks.ok
    assert snapshot["queryScope"]["cardRelationMissingIds"] == [3384]
    if not kwargs.get("bad_fallback"):
        assert not any("FROM WorkItemLinks" in body.get("query", "") for _url, body in calls)


@pytest.mark.parametrize("field,value", [("complete", False), ("sourceId", 999), ("asOf", None)])
def test_full_board_incomplete_or_wrong_fallback_provenance_is_rejected(field, value):
    board, snapshot, _ = synthetic_collection()
    snapshot["queryScope"]["cardRelationFallback"]["observations"]["3384"][field] = value
    body, _ = render.compose(board, snapshot, kids=ado.open_children_of(snapshot))
    assert not validate.check(body, snapshot, expected_children=len(snapshot["children"])).ok


def test_mixed_190_item_batches_keep_offview_endpoint_ids_without_fetching_them():
    link_rows = [{"rel": FORWARD, "source": {"id": 3384}, "target": {"id": 77777}}]
    board, snapshot, calls = synthetic_collection(extra_known=range(10000, 10201), link_rows=link_rows)
    batches = [body["ids"] for _url, body in calls if "ids" in body]
    assert len(batches) >= 2 and len(batches[0]) == 190
    assert all(len(batch) <= 190 for batch in batches)
    assert all(77777 not in batch for batch in batches)
    assert {"predecessor": 3384, "successor": 77777, "observedBy": [3384]} in snapshot["dependencies"]
    body, _ = render.compose(board, snapshot, kids=ado.open_children_of(snapshot))
    assert validate.check(body, snapshot, expected_children=len(snapshot["children"])).ok
    assert "off-view endpoint(s) #77777" in body
    assert (3384, 77777, True) not in template.read_links(body)


def test_fallback_deduplicates_same_edge_observed_in_both_directions():
    queried = []

    def open_replay(request, **_kwargs):
        body = json.loads(request.data)
        if "ids" in body:
            return response({"count": 2, "value": [
                {"id": wid, "fields": {"System.State": "New", "System.WorkItemType": "Feature"}}
                for wid in (10, 20)
            ]})
        if "FROM WorkItemLinks" not in body["query"]:
            return response({"workItems": []})
        source = 10 if "[Source].[System.Id] = 10)" in body["query"] else 20
        queried.append(source)
        payload = fixture("3384-empty-dependency-links.json")
        payload["workItemRelations"] = [{
            "rel": FORWARD if source == 10 else REVERSE,
            "source": {"id": source}, "target": {"id": 20 if source == 10 else 10},
        }]
        return response(payload)

    snapshot = ado.collect([10, 20], [], since="2026-10-29T15:00:00Z",
                           client=ado.Client(token="test-only", _urlopen=open_replay))
    assert queried == [10, 20]
    assert snapshot["dependencies"] == [{"predecessor": 10, "successor": 20, "observedBy": [10, 20]}]


def test_full_board_rejected_attempt_is_retained_latest_only_without_changing_accepted_state(tmp_path, monkeypatch):
    board, accepted, _ = synthetic_collection(literal_3384=True)
    _, rejected, _ = synthetic_collection(bad_fallback=True)
    source = tmp_path / "board.html"
    source.write_text(board, encoding="utf-8")
    paths = get_paths(tmp_path / "data")
    monkeypatch.setattr(ado, "collect", lambda *_args, **_kwargs: accepted)
    assert refresh.run(paths, source=source).ok
    assert refresh.run(paths, source=source).ok  # populate previous accepted state too
    protected = [paths.snapshot, paths.previous, paths.states, paths.artifact, paths.changes]
    before = {path: path.read_bytes() for path in protected}
    rejected = copy.deepcopy(rejected)
    rejected["serverDate"] = "Thu, 29 Oct 2026 15:12:00 GMT"
    rejected["diagnosticOnly"] = "UNACCEPTED-SNAPSHOT-SENTINEL"
    monkeypatch.setattr(ado, "collect", lambda *_args, **_kwargs: rejected)
    result = refresh.run(paths, source=source)
    assert not result.ok
    diagnostic = paths.home / "failed-attempt.latest.json"
    saved = json.loads(diagnostic.read_text())
    assert saved["accepted"] is False and saved["served"] is False
    assert saved["attemptedRevision"] == result.revision
    assert saved["snapshot"] == rejected
    assert saved["validation"] == {"ok": False, "notes": result.checks.notes, "totals": result.checks.totals}
    assert {path: path.read_bytes() for path in protected} == before
    assert "UNACCEPTED-SNAPSHOT-SENTINEL" not in paths.html.read_text(encoding="utf-8")
    assert result.served_revision == render.revision_of(accepted)
    rejected["serverDate"] = "Thu, 29 Oct 2026 15:13:00 GMT"
    result = refresh.run(paths, source=source)
    assert json.loads(diagnostic.read_text())["attemptedRevision"] == result.revision
    assert list(paths.home.glob("failed-attempt*")) == [diagnostic]
    assert {path: path.read_bytes() for path in protected} == before
    diagnostic_before = diagnostic.read_bytes()
    monkeypatch.setattr(ado, "collect", lambda *_args, **_kwargs: accepted)
    assert refresh.run(paths, source=source).ok
    assert diagnostic.read_bytes() == diagnostic_before, "acceptance never consumes the rejected diagnostic"


def test_diagnostic_write_failure_does_not_hide_validation_or_damage_accepted_state(tmp_path, monkeypatch):
    board, accepted, _ = synthetic_collection(literal_3384=True)
    _, rejected, _ = synthetic_collection(bad_fallback=True)
    source = tmp_path / "board.html"
    source.write_text(board, encoding="utf-8")
    paths = get_paths(tmp_path / "data")
    monkeypatch.setattr(ado, "collect", lambda *_args, **_kwargs: accepted)
    assert refresh.run(paths, source=source).ok
    protected = [paths.snapshot, paths.states, paths.artifact, paths.changes]
    before = {path: path.read_bytes() for path in protected}
    write_json = refresh.write_json

    def denied_diagnostic(path, payload):
        if path.name == "failed-attempt.latest.json":
            raise PermissionError("simulated diagnostic storage failure")
        return write_json(path, payload)

    monkeypatch.setattr(refresh, "write_json", denied_diagnostic)
    monkeypatch.setattr(ado, "collect", lambda *_args, **_kwargs: rejected)
    result = refresh.run(paths, source=source)
    assert not result.ok
    assert any("3384" in note for note in result.checks.notes)
    assert "failed-attempt diagnostic write failed (OSError)" in result.checks.notes
    assert result.served_revision == render.revision_of(accepted)
    assert {path: path.read_bytes() for path in protected} == before
