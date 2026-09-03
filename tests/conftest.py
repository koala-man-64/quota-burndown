import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quota_burndown.config import get_paths  # noqa: E402
from quota_burndown.store import Store  # noqa: E402


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTA_BURNDOWN_HOME", str(tmp_path / "home"))
    return get_paths(tmp_path / "home")


@pytest.fixture
def store(paths):
    return Store(paths)


@pytest.fixture
def fixture_dir():
    return Path(__file__).parent / "fixtures"
