#!/usr/bin/env -S uv run --script
import json
import os
import subprocess
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.docker_sandbox import (
    calls,
    cli_peer,
    configure,
    isolated_controller,
    normalize_recording,
    workspace,
)
from support.requirements import POSIX, requires
from support.suites import run_this_suite

TEMPLATE = "docker.io/example/console@sha256:" + "a" * 64


@requires(POSIX)
def test_compute_selection_skips_native_bundle_and_captures_configuration(
    binary: Path,
) -> list:
    with workspace() as root:
        # A relocated executable with no companion bundle. Provider and resolver
        # sentinels fail on invocation; the fake peer only tests orchestration.
        relocated, environment = isolated_controller(binary, root)
        config = configure(root, template=TEMPLATE)
        for flags in ((), ("--no-sandbox",)):
            config = configure(root, template=TEMPLATE)
            with McpClient(relocated, ("serve", *flags), environment, root) as client:
                client.initialize_and_list_tools()
                tool = client.transcript[-1]["result"]["tools"][0]
                assert "requirements" not in tool["inputSchema"]["properties"]
                assert (
                    "microVM" in tool["description"]
                    and "without a sandbox" not in tool["description"]
                )
                client.send(r="42")
                assert last_result_text(client) == "provider peer\n"
                config.write_text("invalid: [")
                client.send(control="restart", r="42")
                assert "provider peer" in last_result_text(client)
                client.send(requirements={"python": ["numpy"]})
                assert "Docker Sandbox targets" in last_result_text(client)
                client.finish()
            assert not (root / "peer/vms").exists()
        assert not (root / "sentinel").exists()
        created = [call["args"] for call in calls(root) if call["args"][0] == "create"]
        assert len(created) == 6
        assert all(args[args.index("--template") + 1] == TEMPLATE for args in created)
        return [
            {
                "fake_provider": True,
                "native_and_resolver_sentinels_invoked": False,
                "captured_template_reused": True,
                "no_sandbox_retains_compute_enforcement": True,
            }
        ]


@requires(POSIX)
def test_unconfirmed_removal_blocks_all_replacements(binary: Path) -> list:
    with workspace() as root:
        environment = cli_peer(root / "peer")
        configure(root, template=TEMPLATE)
        with McpClient(binary, ("serve",), environment, root) as client:
            client.initialize_and_list_tools()
            client.send(r="42")
            (root / "peer/mode").write_text("removal-failed")
            client.send(control="restart", r="must_not_run <- TRUE")
            assert "unconfirmed" in last_result_text(client), last_result_text(client)
            client.send(control="restart", r="must_not_run <- TRUE")
            assert last_result_text(client) == "[worker is shutting down]", (
                last_result_text(client)
            )
            assert sum(call["args"][0] == "create" for call in calls(root)) == 2
            client.stdin.close()
            client.stdout.read(timeout=25)
            client.stderr.read(timeout=25)
            client.process.wait(timeout=5)
        return [
            {"fake_provider": True, "removal_failed": True, "replacement_blocked": True}
        ]


@requires(POSIX)
def test_cancelled_creation_and_probe_retire_owned_resources(binary: Path) -> list:
    records = []
    for mode in ("create-unacknowledged", "create-gate", "probe-gate", "launch-gate"):
        with workspace() as root:
            environment = cli_peer(root / "peer")
            configure(root, template=TEMPLATE)
            (root / "peer/mode").write_text(mode)
            with closing(FifoCheckpoint.create(root / "peer/reached")) as reached:
                with McpClient(binary, ("serve",), environment, root) as client:
                    if mode == "launch-gate":
                        client.initialize_and_list_tools()
                        client.start_request(
                            "tools/call",
                            name="send",
                            arguments={"r": "must_not_run <- TRUE"},
                        )
                    reached.wait("provider operation admitted", timeout=30)
                    client.stdin.close()
                    client.stdout.read(timeout=30)
                    error = client.stderr.read(timeout=30)
                    client.process.wait(timeout=5)
            assert not (root / "peer/vms").exists()
            if mode.startswith("create-"):
                assert "unconfirmed" in error, error
            records.append(
                {
                    "fake_provider": True,
                    "cancelled": mode,
                    "owned_resources_absent": True,
                    "unacknowledged_creation_remains_unconfirmed": mode.startswith(
                        "create-"
                    ),
                }
            )
    return records


@requires(POSIX)
def test_cli_contract_failures_are_noninteractive(binary: Path) -> list:
    records = []
    for mode, expected in (
        ("unsupported-version", "requires supported sbx v0.42.1"),
        ("malformed-listing", "invalid Docker Sandbox listing"),
        ("create-failed", "creation returned no identity"),
    ):
        with workspace() as root:
            environment = cli_peer(root / "peer")
            configure(root, template=TEMPLATE)
            (root / "peer/mode").write_text(mode)
            with McpClient(binary, ("serve",), environment, root) as client:
                assert client.stdout.read(timeout=15) == ""
                errors = client.stderr.read(timeout=15)
                assert client.process.wait(timeout=5) != 0
            assert expected in errors, errors
            invoked = calls(root)
            assert not any(call["args"][0] in ("exec", "rm") for call in invoked), (
                invoked
            )
            assert sum(call["args"][0] == "create" for call in invoked) == (
                mode == "create-failed"
            )
            records += normalize_recording(
                [{"fake_provider": mode, "stderr": errors}], root
            )
    return records


@requires(POSIX)
def test_argument_arrays_and_unrelated_ownership(binary: Path) -> list:
    with workspace() as root:
        environment = cli_peer(root / "peer")
        unrelated = [{"name": "mcp-console-user-owned", "id": "user-owned-identity"}]
        (root / "peer/vms").write_text(json.dumps(unrelated))
        shared = root / 'spaces; "quotes" $(not-a-command)'
        cwd = '/workspace/with spaces; "quotes"'
        configure(
            root,
            template=TEMPLATE,
            workspace=cwd,
            command=["/opt/console with spaces", "--fixture-prefix"],
            mounts=[
                {
                    "source": "nested/../" + shared.name,
                    "target": str(shared),
                    "access": "read_write",
                }
            ],
        )
        with McpClient(binary, ("serve",), environment, root) as client:
            client.initialize_and_list_tools()
            client.send(r="42")
            client.finish()
        invoked = calls(root)
        for call in invoked:
            args = call["args"]
            if args[0] == "create":
                assert args[-2:] == ["shell", str(shared)], args
            if args[0] == "exec":
                assert args[1:4] == ["-i", "--workdir", cwd], args
                assert args[5:7] == ["/opt/console with spaces", "--fixture-prefix"], (
                    args
                )
        assert json.loads((root / "peer/vms").read_text()) == unrelated
        return [{"argv_values_preserved": True, "unrelated_resource_untouched": True}]


@requires(POSIX)
def test_provider_diagnostics_stay_on_controller_stderr(binary: Path) -> list:
    with workspace() as root:
        environment = cli_peer(root / "peer")
        configure(root, template=TEMPLATE)
        (root / "peer/mode").write_text("diagnostics")
        with McpClient(binary, ("serve",), environment, root) as client:
            client.initialize_and_list_tools()
            client.send(r="42")
            assert last_result_text(client) == "provider peer\n"
            transcript, diagnostics = client.finish_with_standard_error()
        assert diagnostics.count("fixture: version diagnostic\n") == 1, diagnostics
        for operation in ("create", "rm"):
            assert diagnostics.count(f"fixture: {operation} diagnostic\n") == 2, (
                diagnostics
            )
            assert diagnostics.count(f"fixture: {operation} progress\n") == 2, (
                diagnostics
            )
        assert "fixture: ls diagnostic\n" in diagnostics, diagnostics
        return transcript[3:] + [
            {"provider_diagnostics_preserved_on_controller_stderr": True}
        ]


if __name__ == "__main__":
    run_this_suite(__file__)
