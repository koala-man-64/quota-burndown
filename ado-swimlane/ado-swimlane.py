"""Launcher: run the CLI straight from this checkout, no install required.

Deliberately no shebang. With `#!/usr/bin/env python3` the Windows `py` launcher
resolves `python3` through the Store app-execution alias, and a process started
that way gets a virtualized AppData - which is where the Azure CLI keeps the
signed-in state this tool authenticates with.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ado_swimlane.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
