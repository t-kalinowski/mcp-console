#!/usr/bin/env python3
import json
import os
import subprocess
import sys

config = {
    "version": 2,
    "filesystem": {
        "kind": "restricted",
        "entries": [
            {"path": {"type": "special", "value": {"kind": "root"}}, "access": "read"}
        ],
    },
    "network": "restricted",
    "environment": {"MESSAGE": 'value: café 雪, "quotes", $() and spaces'},
}
console = sys.argv[1] if len(sys.argv) > 1 else "mcp-console"
subprocess.run(
    [
        console,
        "sandbox",
        "--config-env",
        "SANDBOX_POLICY",
        "--",
        "/bin/sh",
        "-c",
        'printf "%s\\n" "$MESSAGE" "$1"',
        "sh",
        "literal argument: * ; $(echo no)",
    ],
    env={**os.environ, "SANDBOX_POLICY": json.dumps(config, ensure_ascii=False)},
    check=True,
)
