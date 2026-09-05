import json

from quota_burndown.handoff import capture, drain, read


def payload(response="r1", used=12, timestamp="2026-09-05T12:00:00Z"):
    return json.dumps({"session_id": "session-one", "response_id": response, "timestamp": timestamp,
                       "rate_limits": {"five_hour": {"used_percent": used, "window_minutes": 300}}})


def test_capture_is_quota_only_and_dedupes_statusline_redraws(tmp_path):
    assert capture(payload(), tmp_path) is True
    first = read(tmp_path)
    assert len(first) == 1 and first[0]["observation_time_provenance"] == "first_seen"
    assert "transcript" not in json.dumps(first)
    assert capture(payload(), tmp_path) is False
    assert read(tmp_path) == first
    assert capture(payload("r2", 12, "2026-09-05T12:05:00Z"), tmp_path) is False
    assert capture(payload("r2", 13, "2026-09-05T12:05:00Z"), tmp_path) is True
    assert len(read(tmp_path)) == 2


def test_capture_ignores_malformed_or_nonquota_payload_and_restart_preserves(tmp_path):
    assert capture("{", tmp_path) is False
    assert capture(json.dumps({"session_id": "a", "transcript": "sensitive"}), tmp_path) is False
    assert capture(payload(), tmp_path)
    records = drain(tmp_path)
    assert len(records) == 1 and records[0]["quota"]["five_hour"]["used_percentage"] == 12
    assert drain(tmp_path) == records  # watermark survives a collector restart/redraw


def test_sessions_are_isolated_and_partial_files_do_not_break_reading(tmp_path):
    assert capture(payload("one"), tmp_path)
    second = json.loads(payload("two"))
    second["session_id"] = "another-account"
    second["organization_id"] = "other-org"
    assert capture(json.dumps(second), tmp_path)
    directory = tmp_path / "quota-burndown-statusline"
    (directory / "partial.json").write_text("{", encoding="utf-8")
    assert len(read(tmp_path)) == 2


def test_documented_timestampless_payload_is_first_seen_and_never_redraw_fresh(tmp_path):
    item = json.loads(payload()); item.pop("timestamp")
    assert capture(json.dumps(item), tmp_path)
    first = read(tmp_path)[0]
    assert first["observation_time_provenance"] == "first_seen" and first["observed_at"]
    item["context_window"] = {"total_input_tokens": 999, "total_output_tokens": 10}
    assert capture(json.dumps(item), tmp_path) is False
    assert read(tmp_path)[0]["observed_at"] == first["observed_at"]


def test_same_digest_different_account_and_new_session_redraw(tmp_path):
    one = json.loads(payload())
    assert capture(json.dumps(one), tmp_path)
    first = read(tmp_path)[0]
    one["session_id"] = "new-session"
    assert not capture(json.dumps(one), tmp_path)
    assert read(tmp_path)[0]["observed_at"] == first["observed_at"]
    one["organization_id"] = "another-account"
    assert capture(json.dumps(one), tmp_path)
    assert len(read(tmp_path)) == 2


def test_spool_is_bounded_and_oversized_file_ignored(tmp_path):
    from quota_burndown.handoff import handoff_directory
    for n in range(80):
        item = json.loads(payload(used=n))
        item["session_id"] = str(n)
        assert capture(json.dumps(item), tmp_path)
    directory = handoff_directory(tmp_path)
    assert len(list(directory.glob("*.json"))) == 64
    (directory / "oversized.json").write_text(' ' * 9000)
    assert len(read(tmp_path)) <= 64
