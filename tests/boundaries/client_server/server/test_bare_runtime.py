#!/usr/bin/env -S uv run --script

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.client import McpClient
from support.normalization import code
from support.r import r_test_environment
from support.records import TranscriptWithCompanions
from support.suites import run_this_suite

PLATFORMS = {"darwin"}


def test_runs_without_a_resolver_bootstrap(binary: Path) -> TranscriptWithCompanions:
    environment, _ = r_test_environment()
    path = environment.get("PATH")
    assert path is not None, "PATH is required"
    environment["PATH"] = os.pathsep.join(
        entry
        for entry in path.split(os.pathsep)
        if not any((Path(entry) / name).exists() for name in ("ir", "uv", "uvx"))
    )
    environment.pop("RETICULATE_UV", None)
    environment.pop("RETICULATE_PYTHON", None)

    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        library = workspace / "library"
        library.mkdir()
        environment["R_LIBS"] = str(library)
        environment["R_LIBS_SITE"] = str(library)
        environment["R_LIBS_USER"] = str(library)

        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=workspace,
        )
        client._initialize_and_list_tools()
        send = client.transcript[-1]["result"]["tools"][0]
        properties = send["inputSchema"]["properties"]
        assert {"r", "python", "sql"} <= properties.keys(), properties
        assert "requirements" not in properties, properties

        client.send(r="1 + 1")
        assert last_result_text(client) == "[1] 2\n"

        # fmt: r
        r = code(r"""
            conditionMessage(tryCatch(
              library(mcpConsoleDefinitelyMissingPackage),
              error = identity
            ))
            """)
        client.send(r=r)
        missing = last_result_text(client)
        assert "there is no package called" in missing, missing

        result = client.send(requirements={"r": ["praise"]})
        assert result == {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "dynamic environment resolution is unavailable; install `ir` "
                        "or `uv` and restart MCP Console"
                    ),
                }
            ],
            "isError": True,
        }, result
        transcript = client._finish()
        session = next((workspace / ".mcp-console" / "sessions").iterdir())
        events = [
            json.loads(line)
            for line in (session / "internal" / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert events[0]["event"] == "session_started", events[0]
        assert events[0]["dynamic_resolution"] is False, events[0]

        quarto = (session / "transcript.qmd").read_text(encoding="utf-8")
        assert "tidyverse" not in quarto, quarto
        assert "numpy" not in quarto, quarto
        assert "praise" not in quarto, quarto
        quarto = quarto.replace(str(workspace.resolve()), "<workspace>")
        return TranscriptWithCompanions(
            transcript=transcript,
            companions={
                "events.yaml": [
                    {
                        "session_started": {
                            "dynamic_resolution": events[0]["dynamic_resolution"]
                        }
                    }
                ],
                "qmd": quarto,
            },
        )


if __name__ == "__main__":
    run_this_suite(__file__)
