import json

from ado_swimlane import ado


def _raw(item_id, *, parent=None, relations=(), assigned=None, priority=2, rev=7):
    fields = {
        "System.Id": item_id,
        "System.WorkItemType": "Feature" if item_id in (10, 20) else "User Story",
        "System.State": "Active" if item_id == 10 else "New",
        "System.Title": f"Item {item_id}",
        "System.ChangedDate": "2026-10-29T06:00:00Z",
        "Microsoft.VSTS.Common.Priority": priority,
        "System.IterationPath": "AdaptiveAssetAllocation\\Current",
    }
    if parent is not None:
        fields["System.Parent"] = parent
    if assigned is not None:
        fields["System.AssignedTo"] = assigned
    return {"id": item_id, "rev": rev, "fields": fields, "relations": list(relations)}


def test_item_row_normalizes_live_owner_priority_revision_and_link_direction():
    raw = _raw(
        10,
        assigned={"displayName": "Rudy", "uniqueName": "rudy@example.invalid"},
        relations=[
            {"rel": "System.LinkTypes.Dependency-Forward", "url": "https://dev.azure.com/o/_apis/wit/workItems/20"},
            {"rel": "System.LinkTypes.Dependency-Reverse", "url": "https://dev.azure.com/o/_apis/wit/workItems/30"},
            {"rel": "System.LinkTypes.Hierarchy-Forward", "url": "https://dev.azure.com/o/_apis/wit/workItems/40"},
            {"rel": "System.LinkTypes.Dependency-Forward", "url": "not-an-item-url"},
        ],
    )

    row = ado.item_row(raw)

    assert row["revision"] == 7
    assert row["priority"] == 2 and row["priorityReturned"]
    assert row["assignedTo"] == "Rudy" and row["assignedToReturned"]
    assert row["iter"] == "AdaptiveAssetAllocation\\Current" and row["iterationReturned"]
    assert row["relationsFetched"]
    assert row["relations"] == [
        {"predecessor": 10, "successor": 20},
        {"predecessor": 30, "successor": 10},
    ]


def test_batch_requests_relation_expansion_and_narrow_owner_fields():
    seen = {}

    class Response:
        headers = {"Date": "Thu, 29 Oct 2026 06:18:03 GMT"}

        def read(self):
            return json.dumps({"value": []}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake_open(request, timeout=None):
        seen["body"] = json.loads(request.data)
        return Response()

    client = ado.Client(token="test", expiry=2**40, _urlopen=fake_open)
    assert client.batch([10]) == []
    assert seen["body"]["$expand"] == "Relations"
    assert "Microsoft.VSTS.Common.Priority" in seen["body"]["fields"]
    assert "System.AssignedTo" in seen["body"]["fields"]
    assert seen["body"]["errorPolicy"] == "omit"


def test_collect_captures_tracked_ids_scope_and_deduplicated_card_edges():
    calls = []

    class FakeClient:
        server_date = "Thu, 29 Oct 2026 06:18:03 GMT"

        def wiql(self, query):
            calls.append(query)
            if "System.WorkItemType] IN" in query:
                return [10]
            if "System.WorkItemType] = 'Bug'" in query:
                return []
            if "System.ChangedDate" in query:
                return [10]
            if "System.Parent" in query:
                return [31]
            raise AssertionError(query)

        def batch(self, ids):
            assert ids == [10, 20, 31, 40, 99]
            return [
                _raw(10, relations=[{
                    "rel": "System.LinkTypes.Dependency-Forward",
                    "url": "https://dev.azure.com/o/_apis/wit/workItems/20",
                }, {
                    "rel": "System.LinkTypes.Dependency-Forward",
                    "url": "https://dev.azure.com/o/_apis/wit/workItems/777",
                }]),
                _raw(20, relations=[{
                    "rel": "System.LinkTypes.Dependency-Reverse",
                    "url": "https://dev.azure.com/o/_apis/wit/workItems/10",
                }]),
                _raw(31, parent=10),
                _raw(40, parent=99),
                _raw(99),
            ]

    snapshot = ado.collect(
        [10, 20], [99], since="2026-10-29T06:00:00Z",
        tracked_items=[{"id": 40, "parent": 99, "label": "tracked"}],
        client=FakeClient(),
    )

    assert len(calls) == 4 and "[System.Parent] IN (10,20)" in calls[-1]
    assert snapshot["dependencies"] == [
        {"predecessor": 10, "successor": 20, "observedBy": [10, 20]},
        {"predecessor": 10, "successor": 777, "observedBy": [10]},
    ]
    assert snapshot["queryScope"]["relationExpand"] == "Relations"
    assert "not fetched solely" in snapshot["queryScope"]["relationEndpointDetailPolicy"]
    assert snapshot["queryScope"]["cardRelationReturnedIds"] == [10, 20]
    assert snapshot["queryScope"]["trackedIds"] == [40]
    assert snapshot["queryScope"]["trackedReturnedIds"] == [40]
    assert snapshot["queryScope"]["detailMissingIds"] == []
    assert snapshot["trackedItems"] == [{"id": 40, "parent": 99, "label": "tracked"}]
    assert [item["id"] for item in ado.open_children_of(snapshot)[10]] == [31]
