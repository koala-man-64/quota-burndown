"""Execute the board's state presentation against contradictory curated cards."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

BOARD = Path(__file__).resolve().parent.parent / "board" / "board.html"


@pytest.mark.parametrize(
    "state,styled_done,pill,expected_done,expected_removed,hidden",
    [
        ("Active", True, "closed 09-10", False, False, True),
        ("New", True, "closed", False, False, True),
        ("Resolved", True, "closed", False, False, True),
        ("Closed", False, "closed 09-10", True, False, False),
        ("Done", False, "closed", True, False, False),
        ("Removed", True, "closed", False, True, True),
        ("Removed", True, "removed 08-21", False, True, False),
        ("none", False, "", False, False, False),
    ],
)
def test_live_state_controls_badge_color_and_completion_pill(
    state, styled_done, pill, expected_done, expected_removed, hidden
):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to execute the inline UI regression")
    html = BOARD.read_text(encoding="utf-8-sig")
    script = html.split("// Current state presentation:", 1)[1]
    script = script[script.index('document.querySelectorAll'):]
    script = script.split("// End current state presentation.", 1)[0]
    fixture = json.dumps(dict(state=state, done=styled_done, pill=pill))
    harness = r"""
const fixture = FIXTURE;
const classes = new Set(fixture.done ? ['done'] : []);
const pill = {textContent: fixture.pill, hidden: false};
const badges = [];
const card = {
  getAttribute: () => fixture.state,
  classList: {
    contains: name => classes.has(name),
    toggle: (name, on) => on ? classes.add(name) : classes.delete(name)
  },
  querySelectorAll: () => [pill],
  querySelector: () => ({insertAdjacentElement: (_, badge) => badges.push(badge)})
};
const document = {querySelectorAll: () => [card], createElement: () => ({})};
SCRIPT
console.log(JSON.stringify({classes: [...classes], hidden: pill.hidden, badges}));
""".replace("FIXTURE", fixture).replace("SCRIPT", script)
    result = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    assert ("done" in actual["classes"]) == expected_done
    assert ("removed" in actual["classes"]) == expected_removed
    assert actual["hidden"] == hidden
    assert [b["textContent"] for b in actual["badges"]] == ([] if state == "none" else [state])


def test_empty_child_count_does_not_claim_delivery_or_contiguous_phases():
    html = BOARD.read_text(encoding="utf-8-sig")
    assert "fully delivered" not in html
    assert '" · P0–P" + (frontDone - 1)' not in html
    assert "No open children; not delivery proof" in html
