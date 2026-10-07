import http.client
import json
import threading
from datetime import timedelta

import pytest

from quota_burndown.service import CapacityService, make_server
from quota_burndown.plans import CreditObservation, PlanState
from quota_burndown.util import now_utc


@pytest.fixture
def running(paths):
    service = CapacityService(paths, collectors=False)
    service._ledger = lambda: service.stop_event.wait(30)
    server = make_server(service, port=0)
    service.start()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield service, server
    server.shutdown()
    server.server_close()
    service.close()
    thread.join(2)


def request(server, method="GET", payload=None, *, headers=None, raw=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=4)
    body = raw if raw is not None else json.dumps(payload) if payload is not None else None
    fields = {"Content-Type": "application/json", **(headers or {})}
    conn.request(method, "/v1/plans", body, fields)
    response = conn.getresponse()
    data = response.read()
    status = response.status
    conn.close()
    return status, json.loads(data) if response.getheader("Content-Type") == "application/json" else data


def plan():
    now = now_utc()
    return {"amount": "100.125", "starts_at": now.isoformat(),
            "ends_at": (now+timedelta(days=2)).isoformat(), "model": "gpt-6-sol", "speed": "standard"}


def test_durable_save_conflict_and_clear_with_busy_history(running, paths):
    service, server = running
    status, initial = request(server)
    assert status == 200
    payload = {"expected_revision": initial["revision"], "provider": "codex", "plan": plan()}
    status, saved = request(server, "POST", payload)
    assert status == 200
    assert saved["plans"]["codex"]["amount"] == "100.125"
    assert saved["plans"]["codex"]["confirmed_spent"] is None
    assert PlanState(paths.home).snapshot()["plans"]["codex"]["amount"] == "100.125"
    assert request(server, "POST", payload)[0] == 409
    assert request(server, "POST", {"expected_revision": saved["revision"], "provider": "codex", "plan": None})[0] == 200
    assert request(server)[1]["plans"]["codex"] is None


def test_origin_bounded_json_and_storage_failure(running, monkeypatch):
    service, server = running
    assert request(server, "POST", {}, headers={"Origin": "https://evil.example"})[0] == 403
    assert request(server, "POST", raw="x"*4097)[0] == 400
    assert request(server, "POST", raw="{")[0] == 400
    assert request(server, "POST", {"expected_revision": 0, "provider": "unknown", "plan": plan()})[0] == 400
    def failure(_):
        raise OSError("disk unavailable")
    monkeypatch.setattr(service.plans, "_write", failure)
    assert request(server, "POST", {"expected_revision": 0, "provider": "codex", "plan": plan()})[0] == 503
    assert request(server)[1]["plans"]["codex"] is None


def test_cached_forecast_survives_age_change_and_empty_provider(running):
    service, server = running
    service.plans.apply({"expected_revision": 0, "provider": "codex", "plan": plan()})
    service.plans.ingest([CreditObservation("codex", "account", now_utc(), "test", "credits", balance="10")])
    cached = service.plans.snapshot()
    cached["plans"]["codex"]["forecast"] = {"available": True, "note": "calibrated"}
    service._credit_forecast = cached
    status, result = request(server)
    assert status == 200
    assert result["plans"]["codex"]["forecast"]["available"]
    assert result["plans"]["claude"] is None

