#!/usr/bin/env -S uv run --script
"""Configured presentation stays independent of interpreter/provider discovery."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.docker import configure as configure_image
from support.docker_sandbox import configure as configure_template
from support.execution import DIRECT
from support.records import Transcript
from support.suites import run_this_suite


LANGUAGES = ("r,python,sql", "r", "python", "sql", "r,python", "r,sql", "python,sql")
MISSING_UV = (
    "Python sessions without R require `uv` on PATH; "
    "set python in .agents/console/config.yaml to use an existing environment"
)


def test_builtin_configured_language_matrix(binary: Path) -> Transcript:
    return _configured_language_matrix(binary)


def test_prepared_configured_language_matrix(binary: Path) -> Transcript:
    return [
        {"source": source, "matrix": _configured_language_matrix(binary, source)}
        for source in ("image", "template")
    ]


def _configured_language_matrix(binary: Path, source: str | None = None) -> Transcript:
    records: Transcript = []
    for enabled in LANGUAGES:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            if source == "image":
                configure_image(root, "console-presentation:unavailable")
            elif source == "template":
                configure_template(
                    root, template="docker.io/example/console@sha256:" + "a" * 64
                )
            # No runtime or provider is discoverable. Configured fields and text
            # must still be present, without provisioning interpreters or targets.
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
                assert ("Switch languages when useful" in description) == (
                    len(fields) > 1
                )
                for field, guidance in (
                    ("r", "Use R for vectorized data"),
                    ("python", "Use Python when its libraries"),
                    ("sql", "consider DuckDB SQL first"),
                ):
                    assert (guidance in description) == (field in fields)
                if enabled == LANGUAGES[0]:
                    baseline = tool
                    # Existing canonical snapshots own the managed field text.
                    # Prepared fields have distinct text, pinned here in full.
                    if source is not None:
                        records.append({"tool": tool})
                else:
                    expected = {
                        field: schema
                        for field, schema in baseline["inputSchema"][
                            "properties"
                        ].items()
                        if field not in {"r", "python", "sql"} or field in fields
                    }
                    assert tool == {
                        **baseline,
                        "description": description,
                        "inputSchema": {
                            **baseline["inputSchema"],
                            "properties": expected,
                        },
                    }
                if source is not None:
                    requirements = properties["requirements"]
                    assert requirements["properties"] == {
                        "action": {"type": "string", "enum": ["get"]}
                    }
                    assert requirements["required"] == ["action"]
                    assert (
                        "Dependency preparation is unavailable on this target."
                        in description
                    )
                    assert 'requirements={"action":"add"' not in description
                    for field in fields:
                        assert properties[field]["description"].endswith(
                            f"Dependencies must be preinstalled in the {source}."
                        )
                # Pin the varying paragraph; compare every shared paragraph and
                # field schema in full so the matrix does not repeat them.
                paragraphs = description.split("\n\n")
                baseline_paragraphs = baseline["description"].split("\n\n")
                assert paragraphs[0] == baseline_paragraphs[0]
                if "sql" in fields:
                    assert paragraphs[2:] == baseline_paragraphs[2:]
                else:
                    assert paragraphs[2:] == baseline_paragraphs[3:]
                records.append(
                    {"languages": enabled, "language_guidance": paragraphs[1]}
                )
                # Observe completed preparation before closing. Discovery stays
                # responsive even when eager startup cannot prepare a runtime.
                result = client.send(timeout_ms=10_000)
                assert result["isError"] is True, result
                assert client.request("tools/list")["result"]["tools"] == [tool]
                _, stderr = client.finish_with_standard_error(expected_exit_status=1)
                if source is None:
                    assert result == {
                        "content": [{"type": "text", "text": MISSING_UV}],
                        "isError": True,
                    }, result
                    assert stderr == MISSING_UV + "\n", stderr
                    if enabled == LANGUAGES[0]:
                        records[-1].update(preparation_error=result, stderr=stderr)
    return records


if __name__ == "__main__":
    run_this_suite(__file__)
