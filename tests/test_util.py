import os
import threading

import pytest

from quota_burndown import util
from quota_burndown.util import atomic_write_text

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows refuses to replace a file another handle holds open")


@windows_only
def test_atomic_write_waits_out_a_reader_holding_the_file(tmp_path):
    """The restart flake: a reader polling capacity-service.json holds it open
    without delete sharing, so os.replace is refused until that handle closes.
    A replacement service crashed on the refusal instead of retrying."""
    path = tmp_path / "capacity-service.json"
    path.write_text("old", encoding="utf-8")
    reader = open(path, encoding="utf-8")  # Python never grants delete sharing
    threading.Timer(0.3, reader.close).start()
    atomic_write_text(path, "new")
    assert path.read_text(encoding="utf-8") == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["capacity-service.json"], "no temp file left behind"


@windows_only
def test_atomic_write_gives_up_on_a_reader_that_never_closes(tmp_path, monkeypatch):
    monkeypatch.setattr(util, "REPLACE_RETRY_S", 0.2)
    path = tmp_path / "capacity-service.json"
    path.write_text("old", encoding="utf-8")
    with open(path, encoding="utf-8"):
        with pytest.raises(PermissionError):
            atomic_write_text(path, "new")
    assert path.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["capacity-service.json"], "no temp file left behind"
