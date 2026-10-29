from ado_swimlane import render, template
from conftest import item, snapshot


def test_closed_reference_is_fetched_after_child_and_change_windows_expire(board):
    source = board.replace('Open thing</div>', 'Open thing</div><div class="n">Task 2893 still New; HTTP 503 in 2026.</div>')
    assert 2893 in template.known_ids(source)
    assert 503 not in template.known_ids(source)
    assert 2026 not in template.known_ids(source)
    current = snapshot([item(2893, "Task", "Closed")], children=(), changed=())
    body, _ = render.compose(source, current, kids={})
    assert "Retained note:" in body
    assert "Task 2893 still New" in body  # retained evidence is not rewritten
    assert "Current ADO: 2893: Closed" in body
    assert "Retained analysis below is historical" in body


def test_missing_reference_never_infers_closed(board):
    source = board.replace('Open thing</div>', 'Open thing</div><div class="n">Task 2893 New.</div>')
    body, _ = render.compose(source, snapshot([]), kids={})
    assert "Current ADO: 2893: unavailable" in body


def test_manual_refresh_obeys_service_writer_lease(tmp_path, monkeypatch):
    from argparse import Namespace
    from ado_swimlane import cli
    from ado_swimlane.util import WriterLease
    called = []
    monkeypatch.setattr(cli.refresh_mod, "run", lambda *a, **k: called.append(True))
    with WriterLease(tmp_path, ".refresh.lock"):
        assert cli._run_refresh(Namespace(home=str(tmp_path), template=None)) == 1
    assert not called


def test_missing_provider_clock_preserves_last_good(tmp_path, board, monkeypatch):
    import pytest
    from ado_swimlane import ado, refresh
    from ado_swimlane.config import Paths
    paths = Paths(tmp_path)
    paths.html.write_text("last good")
    source = tmp_path / "board.html"
    source.write_text(board)
    monkeypatch.setattr(ado, "collect", lambda *a, **k: snapshot([], server="invalid"))
    with pytest.raises(ado.AdoError, match="valid server Date"):
        refresh.run(paths, source=source)
    assert paths.html.read_text() == "last good"
    assert not paths.snapshot.exists()
