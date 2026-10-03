#!/usr/bin/env -S uv run --script
import json
import os
import select
import signal
import subprocess
import sys
from contextlib import ExitStack, closing
from pathlib import Path
from tempfile import TemporaryDirectory

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
from support.events import Events
from support.native import LOADER_VARIABLE, build_interposer
from support.requirements import NATIVE_FIXTURES, POSIX, PROCESS_EVENTS, requires
from support.suites import run_this_suite

TEMPLATE = "docker.io/example/console@sha256:" + "a" * 64


@requires(POSIX)
def test_minimum_and_newer_cli_versions(binary: Path) -> list:
    records = []
    for version in ("0.42.1", "0.42.2", "0.45.1", "0.100.0", "1.0.0"):
        with workspace() as root:
            environment = cli_peer(root / "peer")
            configure(root, template=TEMPLATE)
            (root / "peer/version").write_text(f"sbx version: v{version} fixture")
            with McpClient(binary, ("serve",), environment, root) as client:
                client.initialize_and_list_tools()
                client.send(r="42")
                assert last_result_text(client) == "provider peer\n"
                client.finish()
            assert not (root / "peer/vms").exists()
            records.append(
                {"fake_provider_version": version, "evaluation_completed": True}
            )
    return records


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
                assert tool["inputSchema"]["properties"]["requirements"]["properties"][
                    "action"
                ]["enum"] == ["get"]
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
                (session,) = (root / ".agents/console/sessions").iterdir()
                diagnostics = (session / "outputs/session.log").read_text()
                assert "unconfirmed" in diagnostics, diagnostics
                expected = (
                    "creation returned no identity"
                    if mode == "create-unacknowledged"
                    else "removal was observed, but creation was not acknowledged"
                )
                assert expected in diagnostics, diagnostics
                assert error == "Docker Sandbox setup cancelled\n", error
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


@requires(POSIX, NATIVE_FIXTURES, PROCESS_EVENTS)
def test_signal_wakeups_coalesce_during_setup_and_retirement(binary: Path) -> list:
    records = []
    # Signal publication and cancellation belong to the Console owner process,
    # outside the fake microVM boundary. No real provider is needed here.
    with TemporaryDirectory(dir="/tmp") as native:
        interposer = build_interposer(Path(native), "target_owner_signals")
        for phase in (
            "installation",
            "interrupted-write",
            "creation",
            "blocked-output",
        ):
            with workspace() as root, ExitStack() as stack:
                installation_phase = phase in ("installation", "interrupted-write")
                environment = cli_peer(root / "peer")
                configure(root, template=TEMPLATE)
                checkpoints = {
                    name: stack.enter_context(
                        closing(FifoCheckpoint.create(root / "peer" / name))
                    )
                    for name in (
                        "installed",
                        "full",
                        "returned",
                        "release",
                        "retirement",
                        "blocked",
                        "reached",
                    )
                }
                environment[LOADER_VARIABLE] = str(interposer)
                environment["MCP_CONSOLE_TEST_OWNER_SIGNALS"] = str(root / "peer")
                if installation_phase:
                    environment["MCP_CONSOLE_TEST_SIGNAL_INSTALLATION"] = "1"
                    (root / "peer/mode").write_text("create-unacknowledged")
                    if phase == "interrupted-write":
                        environment["MCP_CONSOLE_TEST_SIGNAL_EINTR"] = "1"
                else:
                    (root / "peer/mode").write_text(
                        "signal-create" if phase == "creation" else "signal-output"
                    )
                stopped = False
                with McpClient(binary, ("serve",), environment, root) as client:
                    try:
                        client.initialize_and_list_tools()
                        if installation_phase:
                            checkpoints["installed"].wait(
                                "handlers installed before watcher"
                            )
                        else:
                            if phase == "blocked-output":
                                pending = client.start_request(
                                    "tools/call", name="send", arguments={"r": "42"}
                                )
                            checkpoints["reached"].wait(
                                "provider setup/output checkpoint"
                            )
                        owner = int((root / "peer/pid").read_text())
                        if phase == "blocked-output":
                            os.kill(client.process.pid, signal.SIGSTOP)
                            stopped = True
                            checkpoints["release"].release()
                            checkpoints["blocked"].wait("owner output reached EAGAIN")
                        # Return receipts prevent OS signal merging from being
                        # mistaken for successful handler publication.
                        first_signals = (
                            (signal.SIGTERM,)
                            if phase == "interrupted-write"
                            else (signal.SIGTERM, signal.SIGINT, signal.SIGHUP) * 2
                        )
                        for index, signum in enumerate(first_signals):
                            os.kill(owner, signum)
                            if index == 0 and phase != "interrupted-write":
                                checkpoints["full"].wait("actual wakeup pipe is full")
                            checkpoints["returned"].wait(
                                "handler returned with errno preserved", timeout=5
                            )
                        if installation_phase:
                            checkpoints["release"].release()
                        checkpoints["retirement"].wait(
                            "ordinary owner reached its retirement frame"
                        )
                        assert not (root / "peer/vms").exists()
                        # Cancellation has completed. Handlers still need a live
                        # pipe throughout retirement-frame delivery.
                        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                            os.kill(owner, signum)
                            checkpoints["returned"].wait(
                                "retirement handler returned", timeout=5
                            )
                        if stopped:
                            with Events() as events:
                                events.watch_process(owner)
                                checkpoints["release"].release()
                                assert owner in events.wait(5), (
                                    "owner retirement was not bounded"
                                )
                            os.kill(client.process.pid, signal.SIGCONT)
                            stopped = False
                            client.receive(pending)
                            assert pending["result"]["isError"], pending
                        else:
                            checkpoints["release"].release()
                        client.stdin.close()
                        client.stdout.read(timeout=15)
                        stderr = client.stderr.read(timeout=15)
                        status = client.process.wait(timeout=5)
                        # Controller shutdown attribution can race an unexpected
                        # owner exit. This case owns bounded owner retirement and
                        # retained provider diagnostics; lifecycle cases own attribution.
                        if phase != "blocked-output":
                            assert status == 1, (phase, status, stderr)
                    finally:
                        if stopped and client.process.poll() is None:
                            os.kill(client.process.pid, signal.SIGCONT)
                invoked = calls(root)
                removals = [
                    call["args"][-1] for call in invoked if call["args"][0] == "rm"
                ]
                assert (
                    len(removals)
                    == {
                        "installation": 0,
                        "interrupted-write": 0,
                        "creation": 1,
                        "blocked-output": 2,
                    }[phase]
                )
                assert len(set(removals)) == len(removals)
                assert not select.select(
                    [checkpoints["retirement"].descriptor], [], [], 0
                )[0], "owner attempted more than one retirement frame"
                assert not (root / "peer/vms").exists()
                (session,) = (root / ".agents/console/sessions").iterdir()
                if phase == "creation":
                    diagnostics = (session / "outputs/session.log").read_text()
                    assert (
                        diagnostics.count("fixture: original creation diagnostic\n")
                        == 1
                    )
                    assert (
                        "removal was observed, but creation was not acknowledged"
                        in diagnostics
                    )
                elif phase == "blocked-output":
                    diagnostics = (session / "outputs/call-000001.log").read_text()
                    assert (
                        diagnostics.count("fixture: original attachment diagnostic\n")
                        == 1
                    )
                else:
                    assert stderr == "Docker Sandbox setup cancelled\n", stderr
                record = {
                    "phase": phase,
                    "full_pipe_wakeups_coalesced": phase != "interrupted-write",
                    "interrupted_write_retried": phase == "interrupted-write",
                    "handler_returns_with_errno_preserved": len(first_signals) + 3,
                    "owner_cancellation_and_single_retirement": True,
                    "original_diagnostics_retained": True,
                }
                if phase == "blocked-output":
                    record["original_provider_diagnostic"] = (
                        "fixture: original attachment diagnostic\n"
                    )
                    record["owner_retired_before_controller_resumed"] = True
                else:
                    record["standard_error"] = normalize_recording([stderr], root)[0]
                records.append(record)
    return records


@requires(POSIX)
def test_cli_contract_failures_are_noninteractive(binary: Path) -> list:
    records = []
    for mode, expected in (
        ("unsupported-version", "requires sbx v0.42.1 or newer"),
        ("malformed-listing", "invalid Docker Sandbox listing"),
        ("create-failed", "creation returned no identity"),
    ):
        with workspace() as root:
            environment = cli_peer(root / "peer")
            configure(root, template=TEMPLATE)
            (root / "peer/mode").write_text(mode)
            with McpClient(binary, ("serve",), environment, root) as client:
                startup_error = client.startup_error()
                result = client.send()
                assert result["isError"], result
                client.stdin.close()
                assert client.stdout.read(timeout=15) == ""
                errors = client.stderr.read(timeout=15)
                assert client.process.wait(timeout=5) != 0
            assert expected in startup_error, startup_error
            invoked = calls(root)
            assert not any(call["args"][0] in ("exec", "rm") for call in invoked), (
                invoked
            )
            assert sum(call["args"][0] == "create" for call in invoked) == (
                mode == "create-failed"
            )
            records += normalize_recording(
                [
                    {
                        "fake_provider": mode,
                        "startup_error": startup_error,
                        "stderr": errors,
                    }
                ],
                root,
            )
    return records


@requires(POSIX)
def test_rejects_prior_python_identity_protocol_before_runtime_decode(
    binary: Path,
) -> list:
    with workspace() as root:
        environment = cli_peer(root / "peer")
        configure(root, template=TEMPLATE)
        (root / "peer/mode").write_text("prior-python-metadata-protocol")
        with McpClient(binary, ("serve",), environment, root) as client:
            client.initialize_and_list_tools()
            response = client.send(r="must_not_run <- TRUE")
            assert response["isError"], response
            error = last_result_text(client)
            assert (
                "incompatible Docker Sandbox bootstrap: expected protocol 11" in error
            ), error
            assert "received protocol 10" in error, error
            assert "missing field" not in error, error
            assert not (root / "peer/evaluations").exists()
            client.finish_with_standard_error(expected_exit_status=1)
        assert not (root / "peer/vms").exists()
        return [{"prior_python_identity_rejected_before_decode": True}]


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
def test_provider_diagnostics_are_recorded_with_worker_output(binary: Path) -> list:
    with workspace() as root:
        environment = cli_peer(root / "peer")
        configure(root, template=TEMPLATE)
        (root / "peer/mode").write_text("diagnostics")
        with McpClient(binary, ("serve",), environment, root) as client:
            client.initialize_and_list_tools()
            client.send(r="42")
            text = last_result_text(client)
            assert text.endswith("provider peer\n"), text
            assert text.count("fixture: version diagnostic\n") == 1, text
            assert text.count("fixture: create diagnostic\n") == 2, text
            assert text.count("fixture: create progress\n") == 2, text
            transcript, stderr = client.finish_with_standard_error()
        assert stderr == "", stderr
        (session,) = (root / ".agents/console/sessions").iterdir()
        diagnostics = (session / "outputs/session.log").read_text()
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
            {"provider_diagnostics_preserved_in_session_recording": True}
        ]


@requires(POSIX)
def test_startup_diagnostics_are_owned_before_any_send(binary: Path) -> list:
    with workspace() as root:
        environment = cli_peer(root / "peer")
        configure(root, template=TEMPLATE)
        (root / "peer/mode").write_text("diagnostics-gate")
        with closing(FifoCheckpoint.create(root / "peer/reached")) as reached:
            with McpClient(binary, ("serve",), environment, root) as client:
                client.initialize_and_list_tools()
                reached.wait("provider output drained without a send", timeout=15)
                client.request("ping")
                # Setup cancellation has no compute retirement receipt. Retain
                # its failure while requiring the CLI to exit before shutdown.
                _, stderr = client.finish_with_standard_error(expected_exit_status=1)
                assert stderr == (
                    "Docker Sandbox setup cancelled; install standalone sbx v0.42.1 "
                    "or newer and complete Docker login and policy setup before "
                    "starting Console; see docs/DOCKER_SANDBOX.md\n"
                ), stderr
        (session,) = (root / ".agents/console/sessions").iterdir()
        assert (
            session / "outputs/session.log"
        ).read_text() == "provider startup\n" * 20000
        events = [
            json.loads(line)
            for line in (session / "internal/events.jsonl").read_text().splitlines()
        ]
        assert not any(
            event["event"] in ("tool_call", "cell_output") for event in events
        )
        assert [call["args"] for call in calls(root)] == [["version"]]
        return [
            {
                "recorded_provider_bytes": 340000,
                "tool_calls": 0,
                "setup_cancelled": True,
                "exit_status": 1,
                "standard_error": stderr,
            }
        ]


if __name__ == "__main__":
    run_this_suite(__file__)
