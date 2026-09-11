#!/usr/bin/env -S uv run --script

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript, TranscriptWithCompanions
from support.requirements import SANDBOX, WORKER, requires
from support.resolvers import bare_runtime_environment
from support.suites import run_this_suite


def test_invalid_send_has_no_external_effects(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        fake_bin = temporary / "bin"
        fake_bin.mkdir()
        resolver_probe = fake_bin / "resolver-probe"
        resolver_probe.write_text(
            code(r"""
                #!/bin/sh

                set -eu
                printf 'resolver started\n' >> "$MCP_CONSOLE_TEST_RESOLVER_RECORD"
                exit 97
                """),
            encoding="utf-8",
        )
        resolver_probe.chmod(0o755)
        (fake_bin / "ir").symlink_to(resolver_probe)

        environment = os.environ.copy()
        path = environment.get("PATH")
        assert path is not None, "PATH is required"
        environment["PATH"] = os.pathsep.join((str(fake_bin), path))
        environment["RETICULATE_UV"] = str(resolver_probe)
        resolver_record = temporary / "resolver-record"
        worker_started = temporary / "zod-started"
        environment["MCP_CONSOLE_TEST_RESOLVER_RECORD"] = str(resolver_record)
        environment["MCP_CONSOLE_TEST_ZOD_STARTED"] = str(worker_started)

        with McpClient(
            binary, DIRECT.serve("--worker", str(zod)), environment
        ) as client:
            client.initialize_and_list_tools()
            invalid = (
                {"r": "echo invalid R cell ran", "requirements": {"r": [""]}},
                {
                    "python": "echo invalid Python cell ran",
                    "requirements": {
                        "python": ["example @ https://example.invalid/example.whl"]
                    },
                },
                {
                    "sql": "echo invalid DuckDB cell ran",
                    "requirements": {"duckdb": ["spatial FROM community"]},
                },
            )
            for arguments in invalid:
                result = client.send(**arguments)
                assert result["isError"] is True, result

            assert not resolver_record.exists(), "invalid input started a host resolver"
            assert not worker_started.exists(), "invalid input started or ran a worker"
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_initializes_and_lists_tools(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    companions = {
        "bare.yaml": _initializes_and_lists_tools(binary, execution, bare=True)
    }
    if execution == SANDBOXED:
        companions["proxy.yaml"] = _initializes_and_lists_tools(
            binary, execution, proxy=True
        )
    return TranscriptWithCompanions(
        _initializes_and_lists_tools(binary, execution),
        companions,
    )


def _initializes_and_lists_tools(
    binary: Path, execution: Execution, *, bare: bool = False, proxy: bool = False
) -> Transcript:
    environment = os.environ.copy()
    environment.pop("MCP_CONSOLE_LANGUAGES", None)
    with tempfile.TemporaryDirectory() as library:
        if bare:
            environment = bare_runtime_environment(environment, Path(library))
        workspace = Path(library) / "workspace"
        workspace.mkdir()
        if proxy:
            config = workspace / ".agents/mcp-console.yaml"
            config.parent.mkdir()
            config.write_text("sandbox: {proxy: {enabled: true}}", encoding="utf-8")
        with McpClient(binary, execution.serve(), environment, workspace) as client:
            client.initialize_and_list_tools()
            listed_tools = client.transcript[-1]["result"]["tools"]
            assert [tool["name"] for tool in listed_tools] == ["send"], listed_tools
            send = listed_tools[0]
            control = send["inputSchema"]["properties"]["control"]
            assert control["type"] == "string", control
            assert control["enum"] == ["interrupt", "restart"], control
            send_schema = json.dumps(send["inputSchema"])
            assert '"$defs"' not in send_schema, send["inputSchema"]
            assert '"$ref"' not in send_schema, send["inputSchema"]

            assert not (workspace / ".mcp-console").exists(), workspace
            if bare:
                assert "requirements" not in send["inputSchema"]["properties"]
                return client.finish()
            send_requirements = send["inputSchema"]["properties"]["requirements"]
            assert send_requirements["type"] == ["object", "null"], send_requirements
            assert send_requirements["additionalProperties"] is False, send_requirements
            requirement_properties = send_requirements["properties"]
            assert requirement_properties.keys() == {"duckdb", "r", "python"}
            for requirement in requirement_properties.values():
                assert requirement["type"] == "array", requirement
                assert requirement["maxItems"] == 64, requirement
                assert requirement["default"] == [], requirement
                assert requirement["items"]["type"] == "string", requirement
                assert requirement["items"]["minLength"] == 1, requirement
            assert requirement_properties["duckdb"]["items"]["maxLength"] == 64
            return client.finish()


@requires(SANDBOX)
def test_describes_project_network_access(binary: Path) -> Transcript:
    cases = (
        (
            "restricted",
            "sandbox: {network: restricted}",
            False,
            "cannot directly access the network",
        ),
        (
            "enabled",
            "sandbox: {network: enabled}",
            False,
            "can directly access the network",
        ),
        (
            "proxy",
            "sandbox: {proxy: {enabled: true}}",
            False,
            "network subject to the launcher's proxy settings",
        ),
        (
            "proxy with local binding",
            "sandbox: {proxy: {enabled: true, allowLocalBinding: true}}",
            False,
            "network subject to the launcher's proxy settings",
        ),
        (
            "proxy with network enabled",
            "sandbox: {network: enabled, proxy: {enabled: true}}",
            False,
            "network subject to the launcher's proxy settings",
        ),
        (
            "native network representation",
            "sandbox: {network: {enabled: null}}",
            False,
            "network access governed by the launcher's sandbox settings",
        ),
        (
            "no sandbox",
            "sandbox: {network: restricted}",
            True,
            "without a sandbox, with the server's permissions, including filesystem and network access",
        ),
    )
    transcript: Transcript = []
    for name, source, no_sandbox, expected in cases:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config = workspace / ".mcp-console/config.yaml"
            config.parent.mkdir()
            config.write_text(source, encoding="utf-8")
            arguments = ["serve", "--worker", "unused-worker"]
            if no_sandbox:
                arguments.append("--no-sandbox")
            with McpClient(binary, arguments, current_directory=workspace) as client:
                client.initialize_and_list_tools()
                description = client.transcript[-1]["result"]["tools"][0]["description"]
                assert expected in description, (name, description)
                if not no_sandbox:
                    assert "paths explicitly allowed by the launcher" in description
                    assert "runs outside the sandbox" in description
                config.write_text("invalid: [", encoding="utf-8")
                listed = client.request("tools/list")
                assert listed["result"]["tools"][0]["description"] == description
                client.finish()
                transcript.append({"configuration": name, "description": description})
    return transcript


def test_limits_send_languages_from_environment(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    environment = os.environ.copy()
    environment["MCP_CONSOLE_LANGUAGES"] = "r,sql"
    with tempfile.TemporaryDirectory() as client_directory:
        worker_started = Path(client_directory) / "zod-started"
        environment["MCP_CONSOLE_TEST_ZOD_STARTED"] = str(worker_started)
        with McpClient(
            binary, DIRECT.serve("--worker", str(zod)), environment
        ) as client:
            client.initialize_and_list_tools()

            tools = {
                tool["name"]: tool for tool in client.transcript[-1]["result"]["tools"]
            }
            send_properties = tools["send"]["inputSchema"]["properties"]
            assert send_properties.keys() == {
                "r",
                "sql",
                "control",
                "requirements",
                "stdin",
                "timeout_ms",
            }
            result = client.send(python="raise AssertionError('disabled cell ran')")
            assert result["isError"] is True, result
            assert not worker_started.exists(), worker_started
            return client.finish()


@requires(WORKER)
def test_validates_send_arguments(binary: Path) -> Transcript:
    with McpClient(binary, DIRECT.serve()) as client:
        client.initialize_and_list_tools()
        client.send(
            # fmt: python
            python=code("""
                print("hello")
            """),
            wait_ms=0,
        )
        result = client.send(
            r="1",
            python="1",
            sql="SELECT 1",
            control="restart",
            requirements={"r": ["praise"]},
        )
        assert result["isError"] is True, result
        assert result["content"][0]["text"] == (
            "only one of `r`, `python`, or `sql` may be supplied"
        ), result

        result = client.send(control="prepare")
        assert result["isError"] is True, result
        assert "prepare" in result["content"][0]["text"], result

        result = client.send(requirements={"r": ["tidyverse"]})
        assert result == {
            "content": [{"type": "text", "text": "[prepared]"}],
            "isError": False,
        }, result

        result = client.send(stdin="", requirements={"r": ["tidyverse"]})
        assert result == {
            "content": [{"type": "text", "text": "[prepared]"}],
            "isError": False,
        }, result

        result = client.send(stdin="answer\n", requirements={"r": ["tidyverse"]})
        assert result["isError"] is True, result
        assert result["content"][0]["text"] == (
            "requirements-only `send` performs standalone preparation and cannot also "
            "queue stdin"
        ), result

        result = client.send(
            control="interrupt",
            requirements={"r": ["tidyverse"]},
        )
        assert result["isError"] is True, result
        assert result["content"][0]["text"] == (
            '`requirements` with `control = "interrupt"` requires a code cell'
        ), result

        result = client.send(
            control="restart",
            requirements={"r": ["tidyverse"]},
        )
        assert result.get("isError") is not True, result

        result = client.send(r="stop('cell was run')", requirements={})
        assert result["isError"] is True, result
        assert result["content"][0]["text"] == (
            "at least one of `requirements.r`, `requirements.python`, or "
            "`requirements.duckdb` is required"
        ), result

        result = client.send(
            r="stop('cell was run')",
            requirements={"r": [""]},
        )
        assert result["isError"] is True, result
        assert (
            result["content"][0]["text"] == "R requirement strings must not be empty"
        ), result

        invalid_python = "example @ https://example.invalid/example.whl"
        result = client.send(
            r="stop('cell was run')",
            requirements={"python": [invalid_python]},
        )
        assert result["isError"] is True, result
        assert result["content"][0]["text"] == (
            f"Python requirement `{invalid_python}` is not accepted: host-side managed "
            "resolution accepts named package requirements only"
        ), result

        result = client.send(
            r="stop('cell was run')",
            requirements={"duckdb": ["spatial FROM community"]},
        )
        assert result["isError"] is True, result
        assert result["content"][0]["text"] == (
            "DuckDB extension names must start with a lowercase ASCII letter and "
            "contain only lowercase ASCII letters, digits, and underscores"
        ), result

        client.send(r=None)
        output = client.transcript[-1]["result"]["content"][0]["text"]
        assert output == "\n[idle]", output
        return client.finish()


def test_validates_standalone_requirement_arguments(binary: Path) -> Transcript:
    with McpClient(binary, DIRECT.serve()) as client:
        client.initialize_and_list_tools()
        client.send(requirements={})
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == (
            "at least one of `requirements.r`, `requirements.python`, or "
            "`requirements.duckdb` is required"
        )

        client.send(requirements={"duckdb": ["spatial FROM community"]})
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == (
            "DuckDB extension names must start with a lowercase ASCII letter and "
            "contain only lowercase ASCII letters, digits, and underscores"
        )

        client.send(requirements={"r": [""]})
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == "R requirement strings must not be empty"

        client.send(requirements={"r": ["cli\ndplyr"]})
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == (
            "R requirement strings must not contain NUL or line breaks"
        )

        client.send(
            control="interrupt",
            requirements={"python": ["py-yaml12"]},
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == (
            '`requirements` with `control = "interrupt"` requires a code cell'
        )

        client.send(
            control="restart",
            requirements={},
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == (
            "at least one of `requirements.r`, `requirements.python`, or "
            "`requirements.duckdb` is required"
        )

        client.send(
            control="restart",
            requirements={"r": ["cli\ndplyr"]},
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == (
            "R requirement strings must not contain NUL or line breaks"
        )

        client.send(
            control="restart",
            requirements={"duckdb": ["spatial FROM community"]},
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == (
            "DuckDB extension names must start with a lowercase ASCII letter and "
            "contain only lowercase ASCII letters, digits, and underscores"
        )
        return client.finish()


def test_rejects_interrupt_without_worker(binary: Path) -> Transcript:
    with McpClient(binary, DIRECT.serve()) as client:
        client.initialize_and_list_tools()
        client.send(control="interrupt", timeout_ms=0)
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == "[worker is not running]"
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
