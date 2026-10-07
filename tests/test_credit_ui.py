from datetime import datetime, timezone

from quota_burndown import credit_ui


NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def snapshot():
    return {
        "revision": 4,
        "plans": {
            "codex": {
                "amount": "25.50", "starts_at": "2026-10-07T12:00:00+00:00",
                "ends_at": "2026-10-12T12:00:00+00:00", "model": "gpt-6.1-sol",
                "speed": "standard", "status": "active", "progress_known": True,
                "confirmed_spent": "2.50", "remaining": "23.00",
                "required_rate_per_hour": "0.191666", "original_rate_per_hour": "0.2125",
                "forecast": {"available": True, "windows": []},
            },
            "claude": None,
        },
        "models": {
            "codex": [{"id": "gpt-6.1-sol", "label": "GPT-6.1 Sol", "speeds": ["standard", "fast"]}],
            "claude": [{"id": "claude-sonnet", "label": "Claude Sonnet", "speeds": ["standard"]}],
        },
    }


def test_independent_native_unit_forms_and_saved_values():
    page = credit_ui.html(snapshot(), NOW, live=True)
    assert page.count('<form class="credit-plan"') == 2
    assert 'data-credit-provider="codex"' in page
    assert 'data-credit-provider="claude"' in page
    assert 'Amount (credits)' in page
    assert 'Amount (USD)' in page
    assert 'value="25.50"' in page
    assert 'value="gpt-6.1-sol" selected' in page
    assert '<option value="standard" selected>Standard</option>' in page
    assert 'name="ends_at" type="datetime-local" required' in page
    assert 'name="starts_at" type="datetime-local" required' in page
    assert 'id="credit-plans"' in page
    assert 'id="dashboard-data"' not in page


def test_static_report_controls_are_read_only():
    page = credit_ui.html(snapshot(), NOW, live=False)
    assert 'Static report: open the live dashboard' in page
    assert 'type="submit" disabled' in page
    assert 'class="credit-clear" disabled' in page
    assert 'name="model" required disabled' in page


def test_unsafe_provider_text_is_escaped_in_html_and_script():
    data = snapshot()
    data["models"]["codex"][0]["label"] = '</option><script>alert(1)</script>'
    data["plans"]["codex"]["amount"] = '"><img src=x onerror=alert(1)>'
    page = credit_ui.html(data, NOW, live=True)
    payload = credit_ui.script(data)
    assert '<img src=x' not in page
    assert '<script>alert(1)</script>' not in page
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in page
    assert '</script>' not in payload.lower()
    assert '\\u003c' in payload


def test_script_keeps_measured_quota_distinct_and_handles_unverified_spend():
    script = credit_ui.script(snapshot())
    assert "document.querySelectorAll('article.card[data-provider][data-window]')" in script
    assert "card.appendChild(panel)" in script
    assert "Measured quota above remains 0–100%." in script
    assert "Forecast only: paid spending is unavailable" in script
    assert "Confirmed paid spend:" in script
    assert "Original goal" in script
    assert "Hypothetical: Claude paid usage is disabled." in script


def test_script_refresh_and_revision_conflict_preserve_edits():
    script = credit_ui.script(snapshot())
    assert "expected_revision:revision" in script
    assert "form.dataset.dirty==='true'" in script
    assert "if(!dirty){form.elements.amount.value" in script
    assert "if(error.conflict)refresh()" in script
    assert "response.status===202" in script
    assert "Save queued; waiting for durable confirmation." in script
    assert "document.addEventListener('dashboard:updated'" in script
    assert "window.qbCreditPlans=activeMap(data)" in script
    assert "dispatchEvent(new Event('credit-plans:updated'))" in script


def test_forecast_workload_trajectory_has_distinct_axis_and_safe_dom():
    script = credit_ui.script(snapshot())
    assert "w.calibration_tokens_per_point" in script
    assert "forecast.required_tokens_per_hour_low" in script
    assert "forecast.required_tokens_per_hour_high" in script
    assert "(end-now)/3600000" in script
    assert "lowRate,highRate)*hours/calibration" in script
    assert "highRate)*hours/calibration" in script
    assert "credit-workload-trajectory" in script
    assert "Cumulative forecast workload · quota-equivalent points" in script
    assert "Dashed gold range is projected workload, not observed quota." in script
    assert "Workload trajectory unavailable: token pace or quota calibration is missing." in script
    assert "document.createElementNS('http://www.w3.org/2000/svg','svg')" in script
    assert "e.textContent=value" in script
    assert "card.appendChild(panel)" in script
    assert ".chart').appendChild" not in script
    assert ".credit-forecast-overlay svg" in credit_ui.CSS


def test_saved_start_precision_and_capacity_window_mapping():
    script = credit_ui.script(snapshot())
    assert "start=savedPlan?savedPlan.starts_at:new Date().toISOString()" in script
    assert "replace(/^5h(?=:|$)/,'300m')" in script
    assert "replace(/^7d(?=:|$)/,'10080m')" in script
    assert "item.key===capacityKey" in script
    assert "item.key===capacityKey+':'+provider" in script
    assert "'change',function(event){form.dataset.dirty='true';if(event.target===form.elements.starts_at)" in script
