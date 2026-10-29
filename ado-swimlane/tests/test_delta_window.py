"""The "Recent changes" window rolls from the last render.

The template is never written back, so a window derived from the template's own
dates never moves: the published page read "09-14 -> 10-25" for six weeks.
"""
from ado_swimlane import refresh
from ado_swimlane.config import get_paths


class FakeClient:
    """Enough of ado.Client for refresh.run: every query is empty, and every id
    the template names comes back as a New feature."""

    def __init__(self, server_date):
        self.server_date = server_date

    def wiql(self, query):
        return []

    def batch(self, ids):
        return [{"id": i, "fields": {"System.WorkItemType": "Feature", "System.State": "New",
                                     "System.Title": f"item {i}"}} for i in ids]


def test_window_rolls_from_the_previous_render_not_the_template(board, tmp_path):
    source = tmp_path / "board.html"
    source.write_text(board, encoding="utf-8")
    paths = get_paths(tmp_path / "home")

    refresh.run(paths, client=FakeClient("Mon, 14 Sep 2026 22:15:00 GMT"), source=source)
    refresh.run(paths, client=FakeClient("Wed, 16 Sep 2026 08:00:00 GMT"), source=source)
    page = paths.artifact.read_text(encoding="utf-8")
    assert 'var SNAPSHOT = "2026-09-16"' in page
    assert 'var DELTA_FROM = "09-14"' in page, "the window starts at the previous render's day"

    # A later poll the same day keeps the window rather than shrinking it.
    refresh.run(paths, client=FakeClient("Wed, 16 Sep 2026 17:30:00 GMT"), source=source)
    assert 'var DELTA_FROM = "09-14"' in paths.artifact.read_text(encoding="utf-8")
