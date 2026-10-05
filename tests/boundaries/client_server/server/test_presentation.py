#!/usr/bin/env -S uv run --script
"""Configured presentation stays independent of interpreter discovery."""

import difflib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.execution import DIRECT
from support.records import Transcript
from support.requirements import SQL
from support.snapshots import platform_snapshots
from support.suites import run_this_suite


LANGUAGES = ("r,python,sql", "r", "python", "sql", "r,python", "r,sql", "python,sql")
MISSING_UV = (
    "Python sessions without R require `uv` on PATH; "
    "set python in .agents/console/config.yaml to use an existing environment"
)


@platform_snapshots("win32")
def test_builtin_configured_language_matrix(binary: Path) -> Transcript:
    return _configured_language_matrix(binary)


def _configured_language_matrix(binary: Path) -> Transcript:
    records: Transcript = []
    languages = LANGUAGES if SQL.available else ("r,python", "r", "python")
    for enabled in languages:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = dict(os.environ, PATH="", MCP_CONSOLE_LANGUAGES=enabled)
            for name in ("R_HOME", "RETICULATE_PYTHON", "RETICULATE_UV"):
                environment.pop(name, None)
            with McpClient(
                binary, DIRECT.serve(), environment, root, record_in_project=False
            ) as client:
                client.initialize_and_list_tools()
                tool = client.transcript[-1]["result"]["tools"][0]
                assert client.request("ping")["result"] == {}
                properties = tool["inputSchema"]["properties"]
                fields = set(properties) & {"r", "python", "sql"}
                assert fields == set(enabled.split(",")), fields
                description = tool["description"]
                if os.name == "nt":
                    assert "local execution on Windows" in description, description
                    assert "SQL is not yet supported" in description, description
                else:
                    assert ("Switch languages when useful" in description) == (
                        len(fields) > 1
                    )
                    for field, guidance in (
                        ("r", "Use R for vectorized data"),
                        ("python", "Use Python when its libraries"),
                        ("sql", "consider DuckDB SQL first"),
                    ):
                        assert (guidance in description) == (field in fields)
                if enabled == languages[0]:
                    baseline = tool
                else:
                    expected = {
                        field: schema
                        for field, schema in baseline["inputSchema"][
                            "properties"
                        ].items()
                        if field not in {"r", "python", "sql"} or field in fields
                    }
                    expected_tool = {
                        **baseline,
                        "description": description,
                        "inputSchema": {
                            **baseline["inputSchema"],
                            "properties": expected,
                        },
                    }
                    assert tool == expected_tool, "\n".join(
                        difflib.unified_diff(
                            json.dumps(expected_tool, indent=2).splitlines(),
                            json.dumps(tool, indent=2).splitlines(),
                        )
                    )
                # Pin the varying paragraph; compare every shared paragraph and
                # field schema in full so the matrix does not repeat them.
                if os.name == "nt":
                    assert description == baseline["description"]
                    guidance = description
                else:
                    paragraphs = description.split("\n\n")
                    baseline_paragraphs = baseline["description"].split("\n\n")
                    assert paragraphs[0] == baseline_paragraphs[0]
                    if "sql" in fields:
                        assert paragraphs[2:] == baseline_paragraphs[2:]
                    else:
                        assert paragraphs[2:] == baseline_paragraphs[3:]
                    guidance = paragraphs[1]
                records.append({"languages": enabled, "language_guidance": guidance})
                # Observe completed preparation before closing. Discovery stays
                # responsive even when eager startup cannot prepare a runtime.
                result = client.send(timeout_ms=10_000)
                assert result["isError"] is True, result
                assert client.request("tools/list")["result"]["tools"] == [tool]
                _, stderr = client.finish_with_standard_error(expected_exit_status=1)
                assert result == {
                    "content": [{"type": "text", "text": MISSING_UV}],
                    "isError": True,
                }, result
                assert stderr == MISSING_UV + "\n", stderr
                if enabled == languages[0]:
                    records[-1].update(preparation_error=result, stderr=stderr)
    return records


if __name__ == "__main__":
    run_this_suite(__file__)
