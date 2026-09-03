#!/usr/bin/env python3
"""Launcher: runs the quota_burndown CLI from this checkout without installation."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from quota_burndown.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
