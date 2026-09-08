import http.client
import json
import threading
import time

import pytest

from quota_burndown.capacity import Observation
from quota_burndown.service import CapacityService, WriterLease, make_server
from quota_burndown.util import now_utc
from datetime import timedelta


@pytest.fixture
def live(paths):
    service = CapacityService(paths, collectors=False)
    # Simulate a history worker remaining busy throughout all requests.
    service._ledger = lambda: service.stop_event.wait(30)
    server = make_server(service, port=0)
    service.start()
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    yield service, server
    server.shutdown(); service.close(); server.server_close(); thread.join(2)


def get(server, path="/v1/capacity"):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    connection.request("GET", path)
    response = connection.getresponse()
    data = json.loads(response.read()); connection.close()
    return data


def test_usage_endpoint_publishes_daily_models_and_matching_markup(paths, monkeypatch):
    from quota_burndown import ledger, render
    from quota_burndown.ledger import Event
    conn = ledger.connect(paths.usage_db)
    ledger.upsert(conn, [Event("codex", "codex-desktop", ledger.REQUEST, "model-chart", now_utc(),
                               model="gpt-5.3-codex-spark", total_tokens=1234)])
    conn.close()
    service = CapacityService(paths, collectors=False)
    monkeypatch.setattr(service.stop_event, "wait", lambda timeout: service.stop_event.set())
    service._ledger()  # one actual cache-publication iteration, without collectors
    server = make_server(service, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        data = get(server, "/v1/usage")
        group = next(group for group in data["daily_models"]["providers"] if group["provider"] == "codex")
        assert group["models"][0]["model"] == "gpt-5.3-codex-spark"
        assert group["total_tokens"] == 1234
        assert data["daily_models_html"] == render.daily_models_html(data["daily_models"])
        assert data["daily_models_html"] in service._page.decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def send_reading(service, used=40):
    now = now_utc()
    service.publish([Observation("codex", "a", "codex", "300m", 300, used,
                                now+timedelta(hours=2), now, now, "app-server")])


def test_cached_reads_and_publication_during_busy_ledger(live):
    service, server = live
    started = time.perf_counter(); send_reading(service)
    while get(server)["provider_states"]["codex"] != "observed":
        assert time.perf_counter() - started < 2
        time.sleep(0.01)
    latency = time.perf_counter() - started
    elapsed = []
    for _ in range(100):
        start = time.perf_counter(); snapshot = get(server); elapsed.append((time.perf_counter()-start)*1000)
    p95 = sorted(elapsed)[94]
    print(f"cached HTTP p95={p95:.2f}ms; adapter-to-visible={latency*1000:.2f}ms")
    assert p95 < 100
    assert snapshot["collector_health"]["publication"]["max_latency_ms"] < 2000


def event(server):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    connection.request("GET", "/v1/capacity/events", headers={"Last-Event-ID": "obsolete:900"})
    response = connection.getresponse()
    assert response.status == 200
    return connection, response


def next_snapshot(response):
    while True:
        line = response.readline()
        if line.startswith(b"data: "): return json.loads(line[6:])


def test_stream_initial_change_and_reconnection(live):
    service, server = live
    conn, response = event(server)
    first = next_snapshot(response)
    send_reading(service)
    updated = next_snapshot(response)
    assert updated["revision"] > first["revision"]
    response.close(); conn.close()
    conn, response = event(server)
    again = next_snapshot(response)
    assert again["revision"] >= updated["revision"]
    assert again["instance_id"] == updated["instance_id"]
    response.close(); conn.close()


def test_only_one_writer_and_periodic_delegation(live):
    service, _ = live
    from quota_burndown.cli import run_collect
    with pytest.raises(RuntimeError):
        with WriterLease(service.paths.home): pass
    assert "delegated" in run_collect(service.paths)
    assert not service.paths.codex_state.exists()


def test_policy_and_origin_validation(live):
    service, server = live
    for reserve, expected in [(5, 202), (True, 400), (9, 400)]:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        conn.request("POST", "/v1/policy", json.dumps({"reserve_pct": reserve}), {"Content-Type": "application/json"})
        response = conn.getresponse(); assert response.status == expected
        response.read(); conn.close()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    conn.request("GET", "/v1/capacity", headers={"Origin": "https://evil.example"})
    response = conn.getresponse(); assert response.status == 403
    response.read(); conn.close()
    with pytest.raises(ValueError): make_server(service, host="0.0.0.0", port=0)


def test_full_queue_rejects_new_policy_without_dropping_accepted(paths):
    service = CapacityService(paths, collectors=False)
    for n in range(256): assert service._enqueue(("policy", n, 0))
    assert service._enqueue(("policy", 5, 0)) is False
    assert service.events.get_nowait() == ("policy", 0, 0)


def test_later_observation_persists_for_restart(live):
    from quota_burndown.capacity import CapacityState
    service, server = live
    send_reading(service, used=21)
    # Read the persisted generation after a second, distinct provider observation.
    time.sleep(0.3)
    send_reading(service, used=44)
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        restored = CapacityState(service.paths.home).read()
        pools = [p for p in restored["pools"] if p["provider"] == "codex"]
        if pools and pools[0]["windows"][0]["used_pct"] == 44:
            break
        time.sleep(0.1)
    else:
        pytest.fail("latest observation did not persist within two-second publication interval")
    assert restored["instance_id"] != get(server)["instance_id"]


def test_writer_lease_released_on_process_death(paths):
    import os
    import subprocess
    import sys
    code = "import sys,time; from pathlib import Path; from quota_burndown.service import WriterLease; lease=WriterLease(Path(sys.argv[1])); lease.__enter__(); print('locked',flush=True); time.sleep(30)"
    process = subprocess.Popen([sys.executable, "-B", "-c", code, str(paths.home)], stdout=subprocess.PIPE,
                               text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(RuntimeError):
            with WriterLease(paths.home): pass
    finally:
        process.terminate(); process.wait(timeout=3)
    with WriterLease(paths.home): pass
