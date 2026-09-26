"""Host uv adapter for public sans-R preparation failure and ownership cases."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys


root = Path(os.environ["MCP_CONSOLE_TEST_PREPARATION"])
arguments = sys.argv[1:]
with (root / "resolutions.jsonl").open("a") as stream:
    stream.write(json.dumps(arguments) + "\n")

if (root / "mode").is_file() and (mode := (root / "mode").read_text()) in (
    "bare-python",
    "search-path",
):
    if arguments[:2] == ["python", "list"]:
        if mode == "bare-python":
            requested = os.environ["UV_PYTHON"]
            selected = shutil.which(requested) if "/" not in requested else requested
        else:
            selected = shutil.which("python3", path=os.environ["UV_PYTHON_SEARCH_PATH"])
        subprocess.run([selected, "-c", "pass"], check=True)
        version = sys.version_info
        print(
            json.dumps(
                [
                    {
                        "version": f"{version.major}.{version.minor}.{version.micro}",
                        "version_parts": {
                            "major": version.major,
                            "minor": version.minor,
                            "patch": version.micro,
                        },
                        "path": sys.executable,
                        "symlink": None,
                        "variant": "default",
                        "implementation": "cpython",
                    }
                ]
            )
        )
        sys.exit(0)
    if arguments[:2] == ["cache", "dir"]:
        print(root / "cache")
        sys.exit(0)
    if arguments[:2] == ["python", "dir"]:
        print(root / "python-install")
        sys.exit(0)
    if "run" in arguments:
        Path(arguments[-1]).write_text(sys.executable)
        sys.exit(0)

if "run" in arguments and "py-yaml12" in arguments:
    mode = (root / "mode").read_text()
    if mode == "failure":
        sys.exit("fixture Python resolution failed")
    if mode in ("inspection", "inspection-interrupt"):
        Path(arguments[-1]).write_text(str(root / "invalid-python"))
        sys.exit(0)
    if mode == "unsafe-candidate":
        Path(arguments[-1]).write_text(os.environ["MCP_CONSOLE_TEST_UNSAFE_PYTHON"])
        sys.exit(0)
    if mode == "interrupt":
        signal.signal(
            signal.SIGINT, lambda *_: sys.exit("fixture Python resolution interrupted")
        )
        with (root / "resolver-pid").open("w") as stream:
            stream.write(str(os.getpid()))
        with (root / "started").open("wb", buffering=0) as stream:
            stream.write(b"1")
        signal.pause()
        raise AssertionError("interrupted resolver continued")

real = os.environ["MCP_CONSOLE_TEST_REAL_UV"]
os.execv(real, [real, *arguments])
