#!/usr/bin/env -S uv run --script

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.records import Transcript
from support.suites import run_this_suite


def test_resolves_python_version_over_json(binary: Path) -> Transcript:
    root = Path(__file__).resolve().parents[3]
    with (root / "Cargo.toml").open("rb") as source:
        build = tomllib.load(source)["package"]["version"]
    with TemporaryDirectory() as temporary:
        uv = Path(temporary) / "uv"
        uv.write_text(
            """#!/bin/sh
case "$1 $2" in
  'python list')
    printf '%s\\n' '[{"version":"3.12.7","version_parts":{"major":3,"minor":12,"patch":7},"symlink":null,"variant":"default","implementation":"cpython"}]'
    ;;
  'tool run')
    for last in "$@"; do :; done
    printf '%s' /usr/bin/true > "$last"
    ;;
  *) exit 90 ;;
esac
"""
        )
        uv.chmod(0o755)
        process = subprocess.Popen(
            [binary, "resolve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PATH": str(uv.parent)},
        )
        assert process.stdin is not None
        assert process.stdout is not None

        def send(message: object) -> None:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()

        def receive() -> object:
            return json.loads(process.stdout.readline())

        try:
            send(
                {
                    "Open": {
                        "version": 6,
                        "build": build,
                        "workspace": "",
                        "selections": {"r_home": None, "python": None},
                        "mode": "PythonOnly",
                    }
                }
            )
            hello = receive()
            discovery = receive()
            assert hello == {"Hello": {"version": 6, "build": build}}, hello
            assert discovery["Completed"]["id"] == 0, discovery
            assert discovery["Completed"]["confirmed"] is True, discovery
            send(
                {
                    "Run": {
                        "id": 1,
                        "operation": {"PythonVersion": {"constraints": [">=3.12"]}},
                    }
                }
            )
            resolved = receive()
            assert resolved["Completed"]["result"] == {"Ok": "3.12.7"}, resolved
            assert resolved["Completed"]["confirmed"] is True, resolved
            manifest = {
                "packages": ["six>=1"],
                "python_version": [">=3.12"],
                "exclude_newer": "2026-01-01",
            }
            send(
                {
                    "Run": {
                        "id": 2,
                        "operation": {"Python": {"requirements": manifest, "r": None}},
                    }
                }
            )
            prepared = receive()
            assert prepared["Completed"]["result"] == {
                "Ok": {"python": "/usr/bin/true", "requirements": manifest}
            }, prepared
            assert prepared["Completed"]["confirmed"] is True, prepared
            send("Close")
            assert receive() == "Closed"
            process.stdin.close()
            assert process.wait(timeout=10) == 0, process.stderr.read()
            assert process.stderr.read() == ""
            help_text = subprocess.run(
                [binary, "--help"], capture_output=True, text=True, check=True
            ).stdout
            assert "  resolve" not in help_text
            return [
                {
                    "hidden_command": "resolve",
                    "resolved_python": "3.12.7",
                    "prepared_manifest": prepared["Completed"]["result"]["Ok"][
                        "requirements"
                    ],
                }
            ]
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


if __name__ == "__main__":
    run_this_suite(__file__)
