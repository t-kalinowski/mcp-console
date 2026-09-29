"""Prepared-runtime framing through the public MCP boundary; no provider acceptance claims."""

import json
import sys
from pathlib import Path
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.checkpoints import FifoCheckpoint
from support.assertions import last_result_text
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
            assert "/target-only/lib/libpython.so" not in tool["description"]
            client.send(python="42")
            assert last_result_text(client) == "provider peer\n"
            client.request("tools/list")
            assert client.transcript[-1]["result"]["tools"][0] == tool
            client.send(requirements={"action": "get"})
            retained = client.transcript[-1]["result"]["structuredContent"]
            assert retained["requirements"]["python"] == []
            client.finish()
        assert not (root / "peer/vms").exists()
        return client.transcript + [
            {"framed_native_capability": True, "target_paths_remain_metadata": True}
        ]


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
            assert "r" in properties and "sql" in properties
            assert "python" in properties
            assert "when available" in properties["python"]["description"]
            result = client.send(python="raise AssertionError('unavailable cell ran')")
            assert result["isError"], result
            assert last_result_text(client) == (
                "[Python cells are unavailable: the session has no Python runtime]"
            ), result
            client.finish()
        assert not (root / "peer/vms").exists()
        return client.transcript


@requires(POSIX)
def test_invalid_probe_results_preserve_mcp_readiness(binary: Path) -> list:
    records = []
    for mode, expected in (
        ("probe-version", "expected protocol 8"),
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
                client.initialize_and_list_tools()
                result = client.send(r="must_not_run <- TRUE")
                assert result["isError"], result
                diagnostics = last_result_text(client)
                assert expected in diagnostics, diagnostics
                client.request("ping")
                client.finish()
            assert not (root / "peer/vms").exists()
            operations = calls(root)
            assert sum(call["args"][0] == "create" for call in operations) == 1
            assert sum(call["args"][0] == "rm" for call in operations) == 1
            records.append({"mode": mode, "diagnostic": diagnostics})
    return records


@requires(POSIX)
def test_protocol_and_eof_remain_available_during_target_setup(binary: Path) -> list:
    records = []
    for mode in ("create-gate", "probe-gate", "launch-gate"):
        with workspace() as root:
            environment = cli_peer(root / "peer")
            configure(root, template=TEMPLATE)
            (root / "peer/mode").write_text(mode)
            with closing(FifoCheckpoint.create(root / "peer/reached")) as reached:
                with McpClient(binary, ("serve",), environment, root) as client:
                    reached.wait("automatic target warmup", timeout=30)
                    client.initialize_and_list_tools()
                    schema = client.transcript[-1]["result"]
                    client.request("tools/list")
                    assert client.transcript[-1]["result"] == schema
                    client.send(requirements={"action": "get"})
                    client.request("ping")
                    client.stdin.close()
                    client.stdout.read(timeout=15)
                    errors = client.stderr.read(timeout=15)
                    client.process.wait(timeout=5)
            assert not (root / "peer/vms").exists()
            operations = calls(root)
            assert sum(call["args"][0] == "create" for call in operations) == (
                2 if mode == "launch-gate" else 1
            )
            records.append(
                {
                    "phase": mode,
                    "protocol_available": True,
                    "owned_resources_retired": True,
                    "unconfirmed_create": "unconfirmed" in errors,
                }
            )
    return records


if __name__ == "__main__":
    run_this_suite(__file__)
