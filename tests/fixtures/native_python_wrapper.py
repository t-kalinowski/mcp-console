"""Run a different Python image while reporting the selected wrapper path."""

import os
import sys
from pathlib import Path

selected = sys.argv[0]
os.environ["MCP_CONSOLE_SELECTED_WRAPPER"] = selected
os.environ["PYTHONPATH"] = str(Path(selected).parent)
os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
