"""Prepared-runtime framing through the public MCP boundary; no provider acceptance claims."""

import json
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.checkpoints import FifoCheckpoint
from support.assertions import last_result_text, wait_for_evaluation_output
from support.docker_sandbox import calls, cli_peer, configure, workspace
from support.requirements import POSIX, requires
from support.suites import run_this_suite

TEMPLATE = "docker.io/example/console@sha256:" + "a" * 64


@requires(POSIX)
def test_empty_python_selection_describes_target_defaults(binary: Path) -> list:
    records = []
    for kind, target in (
        ("local", None),
        ("ssh", {"transport": {"kind": "ssh", "host": "unused"}}),
        ("docker", {"compute": {"kind": "docker", "image": "unused"}}),
        (
            "docker_sandbox",
            {"compute": {"kind": "docker_sandbox", "template": TEMPLATE}},
        ),
    ):
        with workspace() as root:
            environment = cli_peer(root / "peer")
            config = root / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps(
                    {
                        "python": "",
                        **(
                            {"target": {"workspace": "/workspace", **target}}
                            if target
                            else {}
                        ),
                    }
                )
            )
            with McpClient(binary, ("serve",), environment, root) as client:
                assert client.stdout.read(timeout=10) == ""
                error = client.stderr.read(timeout=10)
                expected = (
                    "preinstalled target Python"
                    if kind in ("docker", "docker_sandbox")
                    else "use uv"
                )
                assert expected in error, error
                assert client.process.wait(timeout=5) != 0
            assert not (root / "peer/calls").exists(), (
                "invalid selection reached provider setup"
            )
            records.append({"target": kind, "error": error})
    return records


@requires(POSIX)
def test_native_probe_projects_capabilities_without_controller_paths(
    binary: Path,
) -> list:
    with workspace() as root:
        environment = cli_peer(root / "peer")
        configure(root, template=TEMPLATE)
        (root / "peer/mode").write_text("native-probe")
        with McpClient(binary, ("serve",), environment, root) as client:
            client.initialize_and_list_tools()
            tool = client.transcript[-1]["result"]["tools"][0]
            assert {"r", "python", "sql"} <= tool["inputSchema"]["properties"].keys(), (
                tool
            )
            assert "/target-only/lib/libpython.so" not in tool["description"], tool[
                "description"
            ]
            client.send(requirements={"action": "get"})
            retained = client.transcript[-1]["result"]["structuredContent"]
            assert retained["requirements"]["python"] == []
            client.finish()
        assert not (root / "peer/vms").exists()
        return client.transcript + [
            {"framed_native_capability": True, "target_paths_remain_metadata": True}
        ]


@requires(POSIX)
def test_prepared_bootstrap_withholds_and_runs_first_cell_once(binary: Path) -> list:
    with workspace() as root:
        environment = cli_peer(root / "peer")
        configure(root, template=TEMPLATE)
        (root / "peer/mode").write_text("bootstrap-input")
        with (
            closing(FifoCheckpoint.create(root / "peer/prompt-ready")) as prompt,
            McpClient(binary, ("serve",), environment, root) as client,
        ):
            client.initialize_and_list_tools()
            # A short deadline admits the cell while the prepared worker's
            # startup prompt blocks initialization. Polling must not replay it.
            client.send(python="first_cell = 42", timeout_ms=0)
            prompt.wait("prepared worker emitted its startup prompt")
            wait_for_evaluation_output(
                client,
                '[input requested: "target startup> "]\n[waiting for stdin]',
                "prepared worker startup prompt",
                timeout_ms=10,
            )
            client.send(timeout_ms=0)
            assert last_result_text(client) == "\n[waiting for stdin]"
            assert not (root / "peer/evaluations").exists()
            wait_for_evaluation_output(
                client, "provider peer\n", "prepared first cell", stdin="continue\n"
            )
            evaluations = (root / "peer/evaluations").read_text().splitlines()
            assert [json.loads(line) for line in evaluations] == [
                {"kind": "evaluate", "language": "python", "source": "first_cell = 42"}
            ]
            client.finish()
        assert not (root / "peer/vms").exists()
        return client.transcript[3:]


@requires(POSIX)
def test_r_only_probe_projects_optional_python(binary: Path) -> list:
    with workspace() as root:
        environment = cli_peer(root / "peer")
        environment.pop("MCP_CONSOLE_LANGUAGES", None)
        configure(root, template=TEMPLATE)
        (root / "peer/mode").write_text("r-only-probe")
        with McpClient(binary, ("serve",), environment, root) as client:
            client.initialize_and_list_tools()
            tool = client.transcript[-1]["result"]["tools"][0]
            properties = tool["inputSchema"]["properties"]
            assert {"r", "python", "sql"} <= properties.keys()
            assert (
                "Language fields describe the configured interface"
                in tool["description"]
            )
            assert (
                "When both runtimes and their bridge are available"
                in properties["r"]["description"]
            )
            result = client.send(python="raise AssertionError('unavailable cell ran')")
            assert result["isError"], result
            assert last_result_text(client) == (
                "Python cells are unavailable: the target has no Python runtime"
            ), result
            client.finish()
        assert not (root / "peer/vms").exists()
        return client.transcript


@requires(POSIX)
def test_presentation_is_independent_of_prepared_runtime(binary: Path) -> list:
    tools = []
    with workspace() as root:
        for mode in ("native-probe", "r-only-probe"):
            environment = cli_peer(root / mode)
            environment.pop("MCP_CONSOLE_LANGUAGES", None)
            configure(root, template=TEMPLATE)
            (root / mode / "mode").write_text(mode)
            with McpClient(binary, ("serve",), environment, root) as client:
                client.initialize_and_list_tools()
                tools.append(client.transcript[-1]["result"]["tools"])
                sql_description = tools[-1][0]["inputSchema"]["properties"]["sql"][
                    "description"
                ]
                assert "during background startup" in sql_description, sql_description
                assert "first-query work remains deferred" in sql_description, (
                    sql_description
                )
                client.send(requirements={"action": "get"})
                client.finish()
    assert tools[0] == tools[1]
    return [{"same_configured_tools_for_r_and_python_targets": True}]


@requires(POSIX)
def test_rejects_target_without_interpreter_bootstrap_protocol(binary: Path) -> list:
    with workspace() as root:
        environment = cli_peer(root / "peer")
        configure(root, template=TEMPLATE)
        (root / "peer/mode").write_text("prior-bootstrap-protocol")
        with McpClient(binary, ("serve",), environment, root) as client:
            error = client.startup_error()
            assert "expected protocol 11" in error, error
            assert "received protocol 8" in error, error
            client.stdin.close()
            assert client.stdout.read(timeout=30) == ""
            diagnostics = client.stderr.read(timeout=30)
            assert "expected protocol 11" in diagnostics, diagnostics
            assert client.process.wait(timeout=5) != 0
        operations = calls(root)
        executions = [call["args"] for call in operations if call["args"][0] == "exec"]
        assert len(executions) == 1 and executions[0][-1] == "docker-sandbox-probe", (
            executions
        )
        assert not (root / "peer/vms").exists()
        return [
            {"older_target_rejected": True, "probe_retired_before_worker_launch": True}
        ]


@requires(POSIX)
def test_invalid_probe_results_retire_before_worker_startup(binary: Path) -> list:
    records = []
    for mode, expected in (
        ("probe-version", "expected protocol 11"),
        ("probe-build", "incompatible Docker Sandbox bootstrap"),
        ("missing-runtime", "no runtime result"),
        ("duplicate-runtime", "unexpected stdout"),
        ("probe-data", "unexpected stdout"),
        ("probe-extra", "unexpected stdout"),
        ("probe-oversized", "frame exceeds 65536 bytes"),
        ("probe-unconfirmed", "retirement is unconfirmed"),
        ("probe-failed", "probe validation failed"),
        ("probe-closed-output", "launch stream ended before confirmed retirement"),
        ("probe-managed", "cannot contain managed"),
        ("native-managed", "cannot contain another selection or a managed cache"),
        ("native-r-conflict", "R capability differs from its captured selection"),
        ("native-relative", "absolute target path"),
        ("native-prefix", "inconsistent executable or base prefixes"),
        ("native-unknown", "unknown field"),
        ("native-embedding-unknown", "unknown field"),
    ):
        with workspace() as root:
            environment = cli_peer(root / "peer")
            configure(root, template=TEMPLATE)
            (root / "peer/mode").write_text(mode)
            with McpClient(binary, ("serve",), environment, root) as client:
                tool_error = client.startup_error()
                assert expected in tool_error, tool_error
                client.stdin.close()
                assert client.stdout.read(timeout=30) == ""
                diagnostics = client.stderr.read(timeout=30)
                assert expected in diagnostics, diagnostics
                assert "BrokenPipeError" not in diagnostics, diagnostics
                assert client.process.wait(timeout=5) != 0
            assert not (root / "peer/vms").exists()
            operations = calls(root)
            assert sum(call["args"][0] == "create" for call in operations) == 1
            assert sum(call["args"][0] == "rm" for call in operations) == 1
            records.append({"mode": mode, "diagnostic": diagnostics})
    return records


if __name__ == "__main__":
    run_this_suite(__file__)
