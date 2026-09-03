#!/usr/bin/env -S uv run --script

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.records import Transcript
from support.suites import run_this_suite


def request_lifecycle(binary: Path, method: str, **params: object) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._request(method, **params)
    return client._finish()


def test_negotiates_legacy_and_discovers_modern_versions(binary: Path) -> Transcript:
    legacy = request_lifecycle(
        binary,
        "initialize",
        protocolVersion="2025-06-18",
        capabilities={},
        clientInfo={
            "name": "protocol-test",
            "version": "1.0.0",
        },
    )
    modern = request_lifecycle(
        binary,
        "server/discover",
        _meta={
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {
                "name": "protocol-test",
                "version": "1.0.0",
            },
            "io.modelcontextprotocol/clientCapabilities": {},
        },
    )
    return legacy + modern


if __name__ == "__main__":
    run_this_suite(__file__)
