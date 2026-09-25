"""Test-only startup behavior for selected-executable inspection."""

import os
import socket
import sys
import sysconfig
from pathlib import Path

root = Path(__file__).parent
mode = (root / "inspection-mode").read_text(encoding="utf-8").strip()

if mode == "output":
    print("startup output before inspection result")
elif mode in ("missing", "unusable"):
    original_get_config_var = sysconfig.get_config_var

    def fixture_config_var(name: str):
        if name == "LDLIBRARY":
            return (
                "missing-embedding-library.so"
                if mode == "missing"
                else "fake-library.so"
            )
        if mode == "unusable" and name in ("LIBDIR", "PYTHONFRAMEWORKPREFIX"):
            return str(root)
        return original_get_config_var(name)

    sysconfig.get_config_var = fixture_config_var
elif mode == "wait":
    address = (root / "inspection-socket").read_text(encoding="utf-8")
    connection = socket.socket(socket.AF_UNIX)
    connection.connect(address)
    connection.sendall(f"{os.getpid()}\n".encode())
    connection.recv(1)
elif mode == "impersonate":
    sys.executable = os.environ["MCP_CONSOLE_SELECTED_WRAPPER"]
else:
    raise ValueError(f"unknown inspection mode: {mode}")
