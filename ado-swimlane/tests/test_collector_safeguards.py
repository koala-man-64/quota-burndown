"""Collector regressions, entirely offline and confined to temporary state."""
import base64
import io
import json
import subprocess
import traceback
import urllib.error

import pytest

from ado_swimlane import ado, refresh, render, service, template
from ado_swimlane.config import Paths
from ado_swimlane.util import LeaseContentionError, WriterLease
from conftest import item, snapshot


class Response:
    headers = {"Date": "Mon, 14 Sep 2026 22:15:00 GMT"}

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_inherited_pat_uses_basic_auth_without_cli_or_secret_repr(monkeypatch):
    secret = "test-only-pat"
    monkeypatch.setenv("AZURE_DEVOPS_EXT_PAT", secret)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("CLI must not run"))
    requests = []

    def opener(request, **kwargs):
        requests.append(request)
        return Response({"workItems": []})

    client = ado.Client(token="explicit-test-pat", _urlopen=opener)
    assert client.post("https://example.invalid", {}) == {"workItems": []}
    assert requests[0].get_header("Authorization") == "Basic " + base64.b64encode(
        (":" + secret).encode()).decode()
    assert "explicit-test-pat" not in repr(client)
    assert secret not in repr(client)


def test_missing_pat_fails_closed_without_network_or_cli(monkeypatch):
    monkeypatch.delenv("AZURE_DEVOPS_EXT_PAT", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("CLI must not run"))
    client = ado.Client(_urlopen=lambda *a, **k: pytest.fail("network must not run"))
    with pytest.raises(ado.AuthError, match="AZURE_DEVOPS_EXT_PAT is missing") as error:
        client.post("https://example.invalid", {})
    assert "azure-cli-pat" in str(error.value)
    assert "az login" not in str(error.value)


@pytest.mark.parametrize("code", [400, 401, 403, 500])
def test_http_errors_do_not_leak_provider_secrets_to_traceback_or_service(
        tmp_path, monkeypatch, code):
    secret = "test-only-pat"
    encoded = base64.b64encode((":" + secret).encode()).decode()
    monkeypatch.setenv("AZURE_DEVOPS_EXT_PAT", secret)
    monkeypatch.setattr(ado.time, "sleep", lambda *_: None)

    def opener(request, **kwargs):
        raise urllib.error.HTTPError(
            "https://example.invalid/" + secret, code, secret + encoded,
            {}, io.BytesIO(json.dumps({"message": secret + encoded}).encode()))

    client = ado.Client(_urlopen=opener)
    instance = service.SwimlaneService(Paths(tmp_path), publish=False)
    expected = ado.AuthError if code in (401, 403) else ado.AdoError
    try:
        client.post("https://example.invalid", {}, attempts=1)
    except expected as exc:
        instance.fail(exc)
        detail = "".join(traceback.format_exception(exc))
    else:
        pytest.fail("provider error accepted")
    evidence = detail + json.dumps(instance.status) + instance.paths.log.read_text()
    assert secret not in evidence
    assert encoded not in evidence
    assert instance.status["state"] == "error"


def test_provider_message_error_is_sanitized(monkeypatch):
    monkeypatch.setenv("AZURE_DEVOPS_EXT_PAT", "test-only-pat")
    client = ado.Client(_urlopen=lambda *a, **k: Response({"message": "test-only-pat"}))
    with pytest.raises(ado.AdoError, match="ADO returned an error response") as exc:
        client.post("https://example.invalid", {})
    assert "test-only-pat" not in "".join(traceback.format_exception(exc.value))


@pytest.mark.parametrize("existing", [False, True])
def test_invalid_render_preserves_accepted_outputs_and_degrades_served_page(tmp_path, board, monkeypatch, existing):
    paths = Paths(tmp_path / "state")
    source = tmp_path / "board.html"
    source.write_text(board)
    targets = [paths.snapshot, paths.previous, paths.states, paths.changes, paths.artifact]
    if existing:
        paths.home.mkdir()
        for target in targets:
            target.write_text('{"serverDate":"Mon, 14 Sep 2026 22:15:00 GMT"}')
        paths.states.write_text('{}')
    before = {p: p.read_bytes() if p.exists() else None for p in targets}
    snap = snapshot([item(200, "Feature", "Active")])
    monkeypatch.setattr(ado, "collect", lambda *a, **k: snap)
    monkeypatch.setattr(render, "compose", lambda *a, **k: ("<p>broken board</p>", template.Report()))
    result = refresh.run(paths, source=source)
    assert not result.ok
    assert result.served_revision is None
    assert "failed validation" in result.health_detail
    assert {p: p.read_bytes() if p.exists() else None for p in targets} == before
    served = paths.html.read_text()
    assert 'class="render-alert"' in served
    assert "unverified template" in served
    assert "broken board" not in served
    assert 'name="ado-snapshot-at"' not in served


@pytest.mark.parametrize("failure", [ado.AuthError("missing PAT"), ado.AdoError("provider failed")])
def test_collector_reports_provider_errors_and_backs_off(tmp_path, monkeypatch, failure):
    instance = service.SwimlaneService(Paths(tmp_path), interval=5, publish=False)
    waits = []

    def fail_refresh(*a, **k):
        raise failure

    def wait(seconds):
        waits.append(seconds)
        instance.stop_event.set()

    monkeypatch.setattr(refresh, "run", fail_refresh)
    monkeypatch.setattr(instance.stop_event, "wait", wait)
    instance._collector()
    assert instance.status["state"] == "error"
    assert str(failure) in instance.status["detail"]
    assert str(failure) in instance.paths.log.read_text()
    assert waits == [service.ERROR_BACKOFF_S]


def test_lease_contention_is_specific_and_does_not_report_provider_failure(tmp_path, monkeypatch):
    instance = service.SwimlaneService(Paths(tmp_path), interval=300, publish=False)
    waits = []

    def wait(seconds):
        waits.append(seconds)
        instance.stop_event.set()

    monkeypatch.setattr(instance.stop_event, "wait", wait)
    monkeypatch.setattr(refresh, "run", lambda *a, **k: pytest.fail("lease already held"))
    with WriterLease(tmp_path, ".refresh.lock"):
        with pytest.raises(LeaseContentionError):
            with WriterLease(tmp_path, ".refresh.lock"):
                pass
        instance._collector()
    assert instance.status["state"] == "starting"
    assert waits == [30]
    assert not instance.paths.log.exists()


def test_valid_refresh_advances_outputs_and_retains_previous_snapshot(tmp_path, board, monkeypatch):
    paths = Paths(tmp_path)
    source = tmp_path / "board.html"
    source.write_text(board)
    old = snapshot([], server="Sun, 13 Sep 2026 22:15:00 GMT")
    paths.snapshot.write_text(json.dumps(old))
    current = snapshot(
        [item(100, "Feature", "Closed"), item(200, "Feature", "Active"),
         item(300, "Feature", "Active"), item(400, "Epic", "New"),
         item(201, parent=200, state="Active")],
        open_ef=[200, 300], children=[201])
    monkeypatch.setattr(ado, "collect", lambda *a, **k: current)
    result = refresh.run(paths, source=source)
    assert result.ok
    assert json.loads(paths.snapshot.read_text()) == current
    assert json.loads(paths.previous.read_text()) == old
    assert json.loads(paths.states.read_text())["200"]["state"] == "Active"
    assert result.body in paths.html.read_text()
    assert f'<meta name="ado-snapshot-at" content="{result.revision}">' in paths.html.read_text()
    assert paths.artifact.read_text() == result.body
    assert json.loads(paths.changes.read_text())["revision"] == result.revision
