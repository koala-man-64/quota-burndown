"""An idle capacity service must rest: its background work is paced, never back to back.

On a few months of history one ledger pass costs tens of CPU-seconds and a full capacity
state is ~1 MB of JSON. The service once rebuilt its dashboard after every collector credit
report and rewrote unchanged state every two seconds, and kept a core ~80% busy for days.
"""
import time
from datetime import timedelta
from pathlib import Path

from quota_burndown import capacity
from quota_burndown import service as service_module
from quota_burndown.capacity import Observation
from quota_burndown.plans import CreditObservation
from quota_burndown.service import CapacityService
from quota_burndown.util import now_utc


def schedule(monkeypatch, *, active_s, idle_s, duty=1.0, poll_s=0.02):
    # raising=False keeps these tests runnable against code that predates the schedule.
    for name, value in (("LEDGER_ACTIVE_INTERVAL_S", active_s), ("LEDGER_IDLE_INTERVAL_S", idle_s),
                        ("LEDGER_ACTIVE_DUTY", duty), ("LEDGER_IDLE_DUTY", duty), ("LEDGER_POLL_S", poll_s)):
        monkeypatch.setattr(service_module, name, value, raising=False)


def count_passes(monkeypatch, cost_s=0.0):
    """Record the start of every dashboard rebuild; each one takes cost_s."""
    passes = []

    def render_html(*_args, **_kwargs):
        passes.append(time.monotonic())
        time.sleep(cost_s)
        return ""
    monkeypatch.setattr(service_module.render, "render_html", render_html)
    return passes


def wait_until(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def credit(balance="10"):
    return [CreditObservation("codex", "acct", now_utc(), "app-server", "credits", balance=balance)]


def test_collector_credit_reports_do_not_rebuild_the_dashboard(paths, monkeypatch):
    # The Codex app-server reports credits on every quota read. Each report used to wake the
    # ledger, so a pass longer than the report interval ran back to back forever.
    schedule(monkeypatch, active_s=60, idle_s=60)
    passes = count_passes(monkeypatch, cost_s=0.1)
    service = CapacityService(paths, collectors=False).start()
    try:
        wait_until(lambda: passes)
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            service.publish_credits(credit())
            time.sleep(0.05)
        assert service.plans.snapshot()["observations"].get("codex"), "credits still reach the plans"
    finally:
        service.close()
    assert len(passes) == 1


def test_pass_spacing_bounds_the_ledger_duty_cycle(paths, monkeypatch):
    # Even when someone is looking, an expensive pass is followed by a proportionate rest.
    schedule(monkeypatch, active_s=0.01, idle_s=0.01, duty=0.25)
    passes = count_passes(monkeypatch, cost_s=0.1)
    service = CapacityService(paths, collectors=False).start()
    try:
        wait_until(lambda: len(passes) >= 3)
    finally:
        service.close()
    gaps = [later - earlier for earlier, later in zip(passes, passes[1:])]
    assert min(gaps) >= 0.1 / 0.25 * 0.9


def test_idle_ledger_waits_until_a_dashboard_opens(paths, monkeypatch):
    schedule(monkeypatch, active_s=0.2, idle_s=60)
    passes = count_passes(monkeypatch)
    service = CapacityService(paths, collectors=False).start()
    try:
        wait_until(lambda: passes)
        time.sleep(0.5)
        assert len(passes) == 1, "nobody is looking: no rebuild before the idle interval"
        service.touch()  # a dashboard read
        wait_until(lambda: len(passes) == 2, timeout=1.0)
    finally:
        service.close()


def test_saved_plan_still_rebuilds_promptly(paths, monkeypatch):
    schedule(monkeypatch, active_s=60, idle_s=60)
    passes = count_passes(monkeypatch)
    service = CapacityService(paths, collectors=False).start()
    try:
        wait_until(lambda: passes)
        service._page_wakeup.set()  # what the writer does after applying a plan
        wait_until(lambda: len(passes) == 2, timeout=1.0)
    finally:
        service.close()


def recording_writes(monkeypatch, fail=False):
    writes = []
    real = capacity.atomic_write_text

    def atomic_write_text(path, text):
        writes.append((Path(path).name, time.monotonic()))
        if fail and Path(path).name == "capacity-state.json":
            raise OSError("held open by a reader")
        return real(path, text)
    monkeypatch.setattr(capacity, "atomic_write_text", atomic_write_text)
    return writes


def state_writes(writes):
    return [at for name, at in writes if name == "capacity-state.json"]


def quiet_service(paths):
    service = CapacityService(paths, collectors=False)
    service._ledger = lambda: service.stop_event.wait(30)  # isolate the writer
    return service.start()


def test_writer_rewrites_capacity_state_only_after_a_change(paths, monkeypatch):
    monkeypatch.setattr(service_module, "PERSIST_INTERVAL_S", 0.05, raising=False)
    writes = recording_writes(monkeypatch)
    service = quiet_service(paths)
    try:
        wait_until(lambda: state_writes(writes))
        writes.clear()
        time.sleep(2.2)  # longer than the old unconditional two-second rewrite
        assert state_writes(writes) == []
        now = now_utc()
        service.publish([Observation("codex", "a", "codex", "300m", 300, 40.0,
                                     now + timedelta(hours=2), now, now, "app-server")])
        wait_until(lambda: state_writes(writes))
    finally:
        service.close()


def test_failed_state_write_is_retried_at_the_persist_interval(paths, monkeypatch):
    # A reader holding the file open must not turn the writer into a tight retry loop.
    monkeypatch.setattr(service_module, "PERSIST_INTERVAL_S", 0.5, raising=False)
    writes = recording_writes(monkeypatch, fail=True)
    service = quiet_service(paths)
    try:
        time.sleep(1.2)
    finally:
        service.close()
    assert 1 <= len(state_writes(writes)) <= 3
    assert service.state.health["storage"]["state"] == "error"
