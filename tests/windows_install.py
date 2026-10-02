"""Build and accept a Windows wheel and source install in a private environment."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from checkout_workflow import checkout_owner, command_process, wait_process


def run(*command, environment=None):
    with command_process(list(map(str, command)), environment=environment) as process:
        status = wait_process(process)
        if status:
            raise subprocess.CalledProcessError(status, command)


if __name__ == "__main__":
    if sys.platform != "win32":
        raise SystemExit("native Windows installation checks require Windows")
    os.chdir(ROOT)
    with (
        checkout_owner(ROOT),
        tempfile.TemporaryDirectory(prefix="console install ") as temporary,
    ):
        directory = Path(temporary)
        wheels = directory / "wheels"
        venv = directory / "venv"
        run("uv", "build", "--wheel", "--out-dir", wheels)
        (wheel,) = wheels.glob("*.whl")
        run("uv", "venv", "--python", sys.executable, venv)
        python = venv / "Scripts/python.exe"
        run("uv", "pip", "install", "--python", python, wheel)
        environment = os.environ | {
            "MCP_CONSOLE_TEST_BINARY": str(venv / "Scripts/mcp-console.exe")
        }
        run(sys.executable, ROOT / "tests/windows.py", "-v", environment=environment)
        run("uv", "pip", "install", "--reinstall", "--python", python, ROOT)
        run(
            sys.executable,
            ROOT / "tests/windows.py",
            "-v",
            "WindowsConsole.test_python_initializes_before_r",
            "WindowsConsole.test_r_initializes_before_python",
            environment=environment,
        )
