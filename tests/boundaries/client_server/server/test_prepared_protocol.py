"""Prepared-runtime framing through the public MCP boundary; no provider acceptance claims."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
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
            assert "r" not in tool["inputSchema"]["properties"], tool
            assert "/target-only/lib/libpython.so" in tool["description"], tool[
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
def test_invalid_probe_results_retire_before_mcp_readiness(binary: Path) -> list:
    records = []
    for mode, expected in (
        ("probe-version", "expected protocol 6"),
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
