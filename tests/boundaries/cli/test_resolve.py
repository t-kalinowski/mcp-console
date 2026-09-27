#!/usr/bin/env -S uv run --script

import json
import os
import subprocess
import shutil
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
        uv = shutil.which("uv")
        assert uv is not None
        path = Path(temporary) / "path"
        path.mkdir()
        (path / "uv").symlink_to(uv)
        environment = dict(os.environ, PATH=str(path), RETICULATE_UV=uv)
        for name in ("R_HOME", "RETICULATE_PYTHON"):
            environment.pop(name, None)
        selected_version = ".".join(map(str, sys.version_info[:3]))
        process = subprocess.Popen(
            [binary, "resolve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={},
            cwd="/",
        )
        assert process.stdin is not None
        assert process.stdout is not None

        def send(message: object) -> None:
            payload = json.dumps(message).encode()
            process.stdin.write(len(payload).to_bytes(4, "big") + payload)
            process.stdin.flush()

        def receive() -> object:
            size = int.from_bytes(process.stdout.read(4), "big")
            assert 0 < size <= 1024 * 1024, process.stderr.read()
            return json.loads(process.stdout.read(size))

        try:
            send(
                {
                    "Open": {
                        "version": 4,
                        "build": build,
                        "workspace": temporary,
                        "selections": {"r_home": None, "python": None},
                        "no_sandbox": True,
                        "settings": {},
                        "launch": {
                            "no_sandbox": True,
                            "custom_worker": False,
                            "workspace": temporary,
                            "cache_home": None,
                            "settings": {},
                            "readable": [],
                            "environment": [
                                [
                                    {"Unix": list(os.fsencode(name))},
                                    {"Unix": list(os.fsencode(value))},
                                ]
                                for name, value in environment.items()
                            ],
                        },
                    }
                }
            )
            hello = receive()
            discovery = receive()
            assert hello == {"Hello": {"version": 4, "build": build}}, hello
            assert discovery["Completed"]["id"] == 0, discovery
            assert discovery["Completed"]["confirmed"] is True, discovery
            send(
                {
                    "Run": {
                        "id": 1,
                        "operation": {
                            "PythonVersion": {
                                "constraints": ["==" + selected_version],
                                "r": None,
                            }
                        },
                    }
                }
            )
            resolved = receive()
            assert resolved["Completed"]["result"] == {"Ok": selected_version}, resolved
            assert resolved["Completed"]["confirmed"] is True, resolved
            manifest = {
                "packages": ["six>=1"],
                "python_version": ["==" + selected_version],
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
            managed = prepared["Completed"]["result"]["Ok"]
            assert managed["requirements"] == manifest, prepared
            assert managed["native"]["embedding"]["python"] == managed["python"], (
                prepared
            )
            assert Path(managed["python"]).is_file(), prepared
            assert prepared["Completed"]["confirmed"] is True, prepared
            send({"Close": {"release": True}})
            assert receive() == "Closed"
            process.stdin.close()
            assert process.wait(timeout=10) == 0, process.stderr.read()
            help_text = subprocess.run(
                [binary, "--help"], capture_output=True, text=True, check=True
            ).stdout
            assert "  resolve" not in help_text
            return [
                {
                    "hidden_command": "resolve",
                    "resolved_python": "<selected Python version>",
                    "prepared_manifest": {
                        **manifest,
                        "python_version": ["==<selected Python version>"],
                    },
                }
            ]
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


if __name__ == "__main__":
    run_this_suite(__file__)
