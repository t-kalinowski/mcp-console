# /// script
# requires-python = ">=3.11"
# dependencies = ["py-yaml12==0.1.0"]
# ///

import difflib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from yaml12 import format_yaml


root = Path(__file__).resolve().parents[1]
binary = root / "target" / "debug" / "mcp-console"
golden = Path(__file__).with_name("golden") / "server.yaml"


def record_transcript(arguments: list[str]) -> str:
    server = subprocess.Popen(
        [binary, *arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert server.stdin is not None
    assert server.stdout is not None
    assert server.stderr is not None

    messages = []

    def send(message: dict[str, Any]) -> None:
        messages.append({"input": message})
        server.stdin.write(json.dumps(message) + "\n")
        server.stdin.flush()

    def receive() -> None:
        line = server.stdout.readline()
        assert line, "mcp-console stopped before replying"
        messages.append({"output": json.loads(line)})

    send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {
                    "name": "acceptance-test",
                    "version": "1.0.0",
                },
            },
        }
    )
    receive()

    send(
        {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }
    )

    send(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
        }
    )
    receive()

    send(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "console",
                "arguments": {
                    "python": "print('hello')",
                    "wait_ms": 0,
                },
            },
        }
    )
    receive()

    server.stdin.close()
    extra_output = server.stdout.read()
    standard_error = server.stderr.read()
    return_code = server.wait()

    assert return_code == 0, standard_error
    assert extra_output == "", f"unexpected extra output: {extra_output}"
    assert standard_error == "", standard_error

    return format_yaml(messages, multi=True)


transcript = record_transcript([])
serve_transcript = record_transcript(["serve"])

if serve_transcript != transcript:
    sys.stderr.writelines(
        difflib.unified_diff(
            transcript.splitlines(keepends=True),
            serve_transcript.splitlines(keepends=True),
            fromfile="mcp-console",
            tofile="mcp-console serve",
        )
    )
    raise SystemExit("mcp-console and mcp-console serve behaved differently")

if os.environ.get("UPDATE_GOLDEN") == "1":
    golden.parent.mkdir(parents=True, exist_ok=True)
    golden.write_text(transcript, encoding="utf-8")
    print(f"updated {golden.relative_to(root)}")
elif not golden.exists():
    raise SystemExit(
        f"{golden.relative_to(root)} is missing; run UPDATE_GOLDEN=1 ./test"
    )
else:
    expected = golden.read_text(encoding="utf-8")
    if transcript != expected:
        sys.stderr.writelines(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                transcript.splitlines(keepends=True),
                fromfile=str(golden.relative_to(root)),
                tofile="actual",
            )
        )
        raise SystemExit("server transcript differs from its golden snapshot")

    print(f"{golden.relative_to(root)}: ok")
