"""Launcher: runs the quota_burndown CLI from this checkout without installation.

Run it as `py quota-burndown.py ...` (or with an explicit python.exe). There is deliberately
no shebang: with `#!/usr/bin/env python3` the Windows `py` launcher resolves `python3` through
the Store app-execution alias, and a process started that way gets a virtualized AppData in
which the Claude desktop app's usage history does not exist.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from quota_burndown.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
