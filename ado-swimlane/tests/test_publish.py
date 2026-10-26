import argparse
from datetime import datetime, timedelta, timezone

import pytest

from ado_swimlane import cli, publish
from ado_swimlane.config import Paths

NOW = datetime(2026, 9, 14, 22, 0, tzinfo=timezone.utc)


@pytest.fixture
def paths(tmp_path):
    return Paths(tmp_path)


def test_fingerprint_ignores_the_refresh_timestamps(board):
    """Every poll rewrites these three, so counting them would publish forever."""
    later = (board.replace('"2026-09-01"', '"2026-09-14"')
                  .replace('"08-31"', '"09-13"')
                  .replace('"09:00 UTC"', '"22:15 UTC"'))
    assert publish.fingerprint(board) == publish.fingerprint(later)


def test_fingerprint_notices_anything_a_reader_would_see(board):
    assert publish.fingerprint(board) != publish.fingerprint(
        board.replace('id="c200"', 'id="c200" data-state="Active"'))
    assert publish.fingerprint(board) != publish.fingerprint(
        board.replace('[[201,"US","New"', '[[201,"US","Active"'))
    assert publish.fingerprint(board) != publish.fingerprint(
        board.replace("7 open epics/features", "8 open epics/features"))
    # Prose edited by hand in the template republishes too, which is the whole
    # point of the split: a person writes the words, the service ships them.
    assert publish.fingerprint(board) != publish.fingerprint(
        board.replace("no refresh may touch", "no refresh may ever touch"))


def test_identical_content_is_not_worth_a_session(board):
    state = publish.PublishState(fingerprint=publish.fingerprint(board))
    decision = publish.decide(state, board, now=NOW)
    assert not decision.publish and "unchanged" in decision.reason


def test_a_real_change_publishes(board):
    state = publish.PublishState(fingerprint="stale")
    assert publish.decide(state, board, now=NOW).publish


def test_debounce_coalesces_a_burst(board):
    state = publish.PublishState(fingerprint="stale", published_at="2026-09-14T21:55:00Z",
                                 day="2026-09-14", count_today=1)
    decision = publish.decide(state, board, now=NOW, min_interval=600)
    assert not decision.publish and "debounced" in decision.reason
    assert 0 < decision.retry_after <= 600
    # Once the window passes, the same change goes out.
    assert publish.decide(state, board, now=NOW + timedelta(minutes=6), min_interval=600).publish


def test_daily_cap_survives_a_bulk_sweep(board):
    """AB#3526 touched 302 work items in 18 minutes; that must not become 302 sessions."""
    state = publish.PublishState(fingerprint="stale", day="2026-09-14", count_today=24)
    decision = publish.decide(state, board, now=NOW, daily_cap=24)
    assert not decision.publish and "daily cap" in decision.reason
    # A new day resets it.
    assert publish.decide(state, board, now=NOW + timedelta(days=1), daily_cap=24).publish


def test_force_overrides_every_gate(board):
    state = publish.PublishState(fingerprint=publish.fingerprint(board), day="2026-09-14",
                                 count_today=99, published_at="2026-09-14T21:59:00Z")
    assert publish.decide(state, board, now=NOW, force=True).publish


def test_state_round_trips(paths):
    state = publish.PublishState(fingerprint="abc", session_id="sid", count_today=3, day="2026-09-14")
    state.save(paths)
    assert publish.PublishState.load(paths).__dict__ == state.__dict__


def test_run_if_due_records_success_and_leaves_failure_unpublished(paths, board, monkeypatch):
    calls = []

    def fake_publish(_paths, *, label, url=None, state=None, model=None):
        calls.append(label)
        return (True, "Version 47") if len(calls) == 1 else (False, "REFUSED newer version exists")

    monkeypatch.setattr(publish, "publish", fake_publish)

    ok, detail = publish.run_if_due(paths, board, label="first")
    assert ok and detail == "Version 47"
    state = publish.PublishState.load(paths)
    assert state.fingerprint == publish.fingerprint(board) and state.count_today == 1

    changed = board.replace("7 open epics/features", "9 open epics/features")
    ok, detail = publish.run_if_due(paths, changed, label="second", force=True)
    assert not ok and "REFUSED" in detail
    after = publish.PublishState.load(paths)
    assert after.fingerprint == state.fingerprint, "a refused publish must not claim it shipped"
    assert after.count_today == 1
    assert after.last_error


def test_dry_run_publishes_nothing(paths, board, monkeypatch):
    monkeypatch.setattr(publish, "publish", lambda *a, **k: pytest.fail("must not publish"))
    ok, reason = publish.run_if_due(paths, board, label="x", dry_run=True)
    assert not ok and reason.startswith("would publish")


def test_label_fits_the_sixty_character_cap():
    label = publish.label_for("2026-09-14T22:15:00Z")
    assert len(label) <= 60 and "09-14 22:15" in label


def test_bootstrap_forces_once_then_never_again(paths, board, monkeypatch):
    """The headless Artifact tool has no read action, so the first publish of a
    pinned session must force; every later one must not."""
    seen = []

    def fake_run(args, prompt):
        seen.append({"resumed": "--resume" in args, "forces": "force true" in prompt})

        class Done:
            returncode = 0
            stdout = "Version 47"
            stderr = ""

        return Done()

    monkeypatch.setattr(publish, "_claude", lambda: "claude")
    monkeypatch.setattr(publish, "_run", fake_run)

    ok, _ = publish.run_if_due(paths, board, label="first")
    assert ok and seen[-1] == {"resumed": False, "forces": True}
    state = publish.PublishState.load(paths)
    assert state.bootstrapped and state.session_id

    changed = board.replace("7 open epics/features", "9 open epics/features")
    ok, _ = publish.run_if_due(paths, changed, label="second", force=False, min_interval=0)
    assert ok and seen[-1] == {"resumed": True, "forces": False}


def test_an_out_of_band_publish_jams_rather_than_overwrites(paths, board, monkeypatch):
    monkeypatch.setattr(publish, "_claude", lambda: "claude")

    class Done:
        returncode = 0
        stdout = "Version 47"
        stderr = ""

    monkeypatch.setattr(publish, "_run", lambda args, prompt: Done())
    publish.run_if_due(paths, board, label="first")

    class Refused(Done):
        stdout = "REFUSED the live version is newer"

    monkeypatch.setattr(publish, "_run", lambda args, prompt: Refused())
    changed = board.replace("7 open epics/features", "9 open epics/features")
    ok, detail = publish.run_if_due(paths, changed, label="second", min_interval=0)
    assert not ok and "REFUSED" in detail
    assert publish.PublishState.load(paths).fingerprint == publish.fingerprint(board)


def test_explicit_force_re_bootstraps(paths, board, monkeypatch):
    prompts = []
    monkeypatch.setattr(publish, "_claude", lambda: "claude")

    class Done:
        returncode = 0
        stdout = "Version 48"
        stderr = ""

    def fake_run(args, prompt):
        prompts.append("force true" in prompt)
        return Done()

    monkeypatch.setattr(publish, "_run", fake_run)
    publish.run_if_due(paths, board, label="first")
    publish.run_if_due(paths, board, label="again", force=True)
    assert prompts == [True, True], "an explicit force starts a fresh baseline"


def test_publisher_presents_as_desktop_hosted_without_overriding(monkeypatch):
    """Without the entrypoint label the CLI hides the Artifact tool, which is why a
    service started at logon could refresh the page but never publish it."""
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    assert publish._env()["CLAUDE_CODE_ENTRYPOINT"] == "claude-desktop"
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "something-else")
    assert publish._env()["CLAUDE_CODE_ENTRYPOINT"] == "something-else"


def test_a_lost_session_is_replaced_with_a_new_one_not_resumed(paths, board, monkeypatch):
    """--resume on an id that was never used fails with "No conversation found";
    doing that on recovery turned a routine session expiry into a permanent jam."""
    calls = []
    monkeypatch.setattr(publish, "_claude", lambda: "claude")

    def fake_run(args, prompt):
        calls.append(args)

        class Done:
            # First call: the aged-out session. Second: the replacement.
            returncode = 1 if len(calls) == 1 else 0
            stdout = "" if len(calls) == 1 else "Version 50"
            stderr = "No conversation found with session ID: abc"

        return Done()

    monkeypatch.setattr(publish, "_run", fake_run)
    state = publish.PublishState(fingerprint="published-before", session_id="abc", bootstrapped=True)
    state.save(paths)
    ok, _ = publish.run_if_due(paths, board, label="x")
    assert ok
    assert calls[0][-1] == "abc" and "--resume" in calls[0]
    assert "--session-id" in calls[1] and "--resume" not in calls[1]
    assert calls[1][calls[1].index("--session-id") + 1] != "abc"


def test_a_lost_session_after_a_refusal_stays_jammed(paths, board, monkeypatch):
    """Recovery forces, and forcing over someone else's publish is exactly what
    the agreed policy forbids."""
    monkeypatch.setattr(publish, "_claude", lambda: "claude")

    class Failed:
        returncode = 1
        stdout = ""
        stderr = "No conversation found"

    monkeypatch.setattr(publish, "_run", lambda args, prompt: Failed())
    state = publish.PublishState(fingerprint="f", session_id="abc", bootstrapped=True,
                                 last_error="REFUSED the live version is newer")
    state.save(paths)
    ok, detail = publish.run_if_due(paths, board, label="x")
    assert not ok and "publish --force" in detail


def test_auth_failure_is_reported_as_auth_not_as_boilerplate():
    """The CLI prints auth failures on stdout and settings warnings on stderr, so
    a naive `stderr or stdout` shows the warnings and hides the real cause."""

    class Done:
        returncode = 1
        stderr = ("Permission allow rule (..\\..\\.claude\\settings.json): Write(//c/**) is not "
                  "matched by file permission checks - Use Edit(//c/**) instead.")

    detail = publish._failure(Done(), "Failed to authenticate. API Error: 401 sign-in has been revoked.")
    assert "claude /login" in detail and "revoked" in detail
    assert "Permission allow rule" not in detail


def _publish_args(home, force=False):
    return argparse.Namespace(home=str(home), force=force, dry_run=False, label=None)


@pytest.mark.parametrize("force", [False, True])
def test_cli_publish_refuses_a_page_that_fails_its_checks(tmp_path, board, monkeypatch, capsys, force):
    """A refresh writes its page whether or not the checks passed, so the CLI must
    re-run them: a stale Artifact is recoverable, a confidently wrong one is not.
    --force overrides the publish gates, never the checks."""
    Paths(tmp_path).artifact.write_text(board.replace("c200: [[201", "c999: [[201"), encoding="utf-8")
    monkeypatch.setattr(publish, "run_if_due", lambda *a, **k: pytest.fail("must not publish"))
    assert cli._run_publish(_publish_args(tmp_path, force=force)) == 2
    err = capsys.readouterr().err
    assert "fails its own checks" in err and "c999" in err


def test_cli_publish_ships_a_page_that_passes_its_checks(tmp_path, board, monkeypatch):
    Paths(tmp_path).artifact.write_text(board, encoding="utf-8")
    shipped = []
    monkeypatch.setattr(publish, "run_if_due", lambda paths, body, **k: shipped.append(body) or (True, "published"))
    assert cli._run_publish(_publish_args(tmp_path)) == 0
    assert shipped == [board]
