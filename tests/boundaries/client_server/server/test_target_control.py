#!/usr/bin/env -S uv run --script
"""Target setup controls retain their cause and scoped retirement evidence."""

import sys
import signal
from contextlib import ExitStack, closing, contextmanager
from collections.abc import Iterator
from tempfile import TemporaryDirectory
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.docker_sandbox import calls, cli_peer, configure, workspace
from support.native import LOADER_VARIABLE, build_interposer
from support.requirements import NATIVE_FIXTURES, POSIX, requires
from support.suites import run_this_suite

TEMPLATE = "docker.io/example/console@sha256:" + "a" * 64


@contextmanager
def gated_controller(
    binary: Path, mode: str, *, peer_mode: str | None = None
) -> Iterator[tuple[McpClient, Path, dict[str, FifoCheckpoint]]]:
    with (
        workspace() as root,
        TemporaryDirectory(dir="/tmp") as native,
        ExitStack() as stack,
    ):
        environment = cli_peer(root / "peer")
        configure(root, template=TEMPLATE)
        if mode == "signal-shutdown":
            (root / "peer/mode").write_text("version-signal")
            (root / "peer/version-signal").write_text(str(signal.SIGKILL))
        if mode == "control-gate" or mode.startswith("poll-error"):
            (root / "peer/mode").write_text("diagnostics-gate")
        if peer_mode is not None:
            (root / "peer/mode").write_text(peer_mode)
        gates = {
            name: stack.enter_context(
                closing(FifoCheckpoint.create(root / "peer" / name))
            )
            for name in (
                "reached",
                "native-reached",
                "native-release",
                "native-cancel",
                "native-controlled",
            )
        }
        environment.update(
            {
                LOADER_VARIABLE: str(
                    build_interposer(
                        Path(native),
                        "target_probe_exit"
                        if mode.startswith("receipt-")
                        else "target_setup_control",
                    )
                ),
                "MCP_CONSOLE_TEST_TARGET_SETUP": str(root / "peer"),
                "MCP_CONSOLE_TEST_TARGET_FAULT": mode,
            }
        )
        with McpClient(binary, ("serve",), environment, root) as client:
            try:
                client.initialize_and_list_tools()
                yield client, root, gates
            finally:
                gates["native-cancel"].release()
                client.close()


@requires(POSIX)
def test_interrupt_during_target_capture_preserves_reason(binary: Path) -> list:
    with workspace() as root:
        environment = cli_peer(root / "peer")
        configure(root, template=TEMPLATE)
        (root / "peer/mode").write_text("diagnostics-gate")
        with closing(FifoCheckpoint.create(root / "peer/reached")) as reached:
            with McpClient(binary, ("serve",), environment, root) as client:
                client.initialize_and_list_tools()
                reached.wait("version command admitted and diagnostics drained")
                client.send(r="must_not_run <- TRUE", timeout_ms=0)
                client.send(control="interrupt", timeout_ms=10000)
                text = last_result_text(client)
                assert "Docker Sandbox setup interrupted" in text, text
                assert "setup cancelled" not in text, text
                _, stderr = client.finish_with_standard_error(expected_exit_status=1)
                assert "Docker Sandbox setup interrupted" in stderr, stderr
        assert [call["args"] for call in calls(root)] == [["version"]]
        return [{"requested_control": "interrupt", "next_setup_command": False}]


@requires(POSIX)
def test_eof_before_provider_creation_confirms_local_retirement(binary: Path) -> list:
    with workspace() as root:
        environment = cli_peer(root / "peer")
        configure(root, template=TEMPLATE)
        (root / "peer/mode").write_text("diagnostics-gate")
        with closing(FifoCheckpoint.create(root / "peer/reached")) as reached:
            with McpClient(binary, ("serve",), environment, root) as client:
                client.initialize_and_list_tools()
                reached.wait("version command admitted and diagnostics drained")
                _, stderr = client.finish_with_standard_error()
                assert stderr == "", stderr
        assert [call["args"] for call in calls(root)] == [["version"]]
        assert not (root / "peer/vms").exists()
        (session,) = (root / ".agents/console/sessions").iterdir()
        assert (
            session / "outputs/session.log"
        ).read_text() == "provider startup\n" * 20000
        return [{"requested_control": "shutdown", "local_retirement_confirmed": True}]


@requires(POSIX, NATIVE_FIXTURES)
def test_repeated_controls_keep_first_cause_across_setup_stages(binary: Path) -> list:
    records = []
    for mode in ("control-gate", "stage-gate"):
        with gated_controller(binary, mode) as (client, root, gates):
            if mode == "control-gate":
                gates["reached"].wait("version command admitted")
            else:
                gates["native-reached"].wait("captured command exit before retirement")
            client.send(r="must_not_run <- TRUE", timeout_ms=0)
            client.send(control="interrupt", timeout_ms=0)
            if mode == "control-gate":
                gates["native-reached"].wait("accepted interrupt woke command wait")
            client.send(control="interrupt", timeout_ms=0)
            client.stdin.close()
            gates["native-release"].release()
            client.stdout.read(timeout=15)
            stderr = client.stderr.read(timeout=15)
            assert client.process.wait(timeout=5) == 1, stderr
            assert "Docker Sandbox setup interrupted" in stderr, stderr
            assert "setup cancelled" not in stderr, stderr
            assert [call["args"] for call in calls(root)] == [["version"]]
            records.append(
                {"boundary": mode, "first_cause": "interrupt", "probe_launched": False}
            )
    return records


@requires(POSIX, NATIVE_FIXTURES)
def test_poll_failure_stays_an_infrastructure_failure(binary: Path) -> list:
    with gated_controller(binary, "poll-error") as (client, root, gates):
        gates["native-reached"].wait("command exit poll admitted")
        gates["reached"].wait("version command admitted")
        client.send(r="must_not_run <- TRUE", timeout_ms=0)
        gates["native-release"].release()
        client.send(timeout_ms=10000)
        text = last_result_text(client)
        assert "Input/output error" in text, text
        assert "setup cancelled" not in text and "setup interrupted" not in text, text
        _, stderr = client.finish_with_standard_error(expected_exit_status=1)
        assert "Input/output error" in stderr, stderr
        assert [call["args"] for call in calls(root)] == [["version"]]
        return [{"fault": "poll", "control_inferred": False}]


@requires(POSIX, NATIVE_FIXTURES)
def test_shutdown_does_not_hide_a_poll_failure_with_local_cleanup(binary: Path) -> list:
    with gated_controller(binary, "poll-error-shutdown") as (client, root, gates):
        gates["native-reached"].wait("command exit poll admitted")
        gates["reached"].wait("version command admitted")
        client.stdin.close()
        gates["native-controlled"].wait("shutdown accepted by this operation")
        gates["native-release"].release()
        _, stderr = client.finish_with_standard_error(expected_exit_status=1)
        assert "Docker Sandbox setup cancelled" in stderr, stderr
        assert "Input/output error" in stderr, stderr
        assert [call["args"] for call in calls(root)] == [["version"]]
        return [
            {
                "requested_control": "shutdown",
                "poll_failure_retained": True,
                "provider_creation": False,
            }
        ]


@requires(POSIX, NATIVE_FIXTURES)
def test_shutdown_preserves_an_independent_signal_failure(binary: Path) -> list:
    with gated_controller(binary, "signal-shutdown") as (client, root, gates):
        gates["native-reached"].wait("version command exit observed before shutdown")
        client.stdin.close()
        gates["native-controlled"].wait("shutdown accepted after independent exit")
        gates["native-release"].release()
        _, stderr = client.finish_with_standard_error(expected_exit_status=1)
        assert stderr == (
            "Docker Sandbox setup cancelled; Docker Sandbox command failed with "
            "signal: 9 (SIGKILL); install standalone sbx v0.42.1 or newer and "
            "complete Docker login and policy setup before starting Console; "
            "see docs/DOCKER_SANDBOX.md\n"
        ), stderr
        assert [call["args"] for call in calls(root)] == [["version"]]
        assert not (root / "peer/vms").exists()
        return [{"independent_signal": "SIGKILL", "exit_status": 1, "stderr": stderr}]


@requires(POSIX, NATIVE_FIXTURES)
def test_probe_owner_failure_after_receipt_preserves_cleanup_evidence(
    binary: Path,
) -> list:
    records = []
    for mode, workload_error, shutdown in (
        ("receipt-sigkill", False, False),
        ("receipt-exit", False, False),
        ("receipt-sigkill", True, True),
    ):
        with gated_controller(
            binary, mode, peer_mode="probe-failed" if workload_error else None
        ) as (client, root, gates):
            gates["native-reached"].wait("probe owner wrote its complete RETIRED frame")
            client.send(r="must_not_run <- TRUE", timeout_ms=0)
            if shutdown:
                client.stdin.close()
            gates["native-release"].release()
            if not shutdown:
                client.send(timeout_ms=10000)
                text = last_result_text(client)
                assert "Docker Sandbox command failed with" in text, text
            _, stderr = client.finish_with_standard_error(expected_exit_status=1)
            status = (
                "signal: 9 (SIGKILL)"
                if mode == "receipt-sigkill"
                else "exit status: 47"
            )
            assert f"Docker Sandbox command failed with {status}" in stderr, stderr
            if not workload_error:
                assert stderr == f"Docker Sandbox command failed with {status}\n", (
                    stderr
                )
            assert ("probe validation failed" in stderr) == workload_error, stderr
            assert "retirement is unconfirmed" not in stderr, stderr
            assert not (root / "peer/evaluations").exists()
            assert not (root / "peer/vms").exists()
            invoked = [call["args"] for call in calls(root)]
            (name,) = [
                args[args.index("--name") + 1]
                for args in invoked
                if args[0] == "create"
            ]
            assert [args for args in invoked if args[0] == "rm"] == [
                ["rm", "--force", name]
            ]
            records.append(
                {
                    "owner_status": status,
                    "workload_error": workload_error,
                    "shutdown": shutdown,
                    "provider_retirement_confirmed": True,
                    "cell_dispatched": False,
                    "exit_status": 1,
                }
            )
    return records


@requires(POSIX, NATIVE_FIXTURES)
def test_setup_input_failure_is_reported_and_retired(binary: Path) -> list:
    with gated_controller(binary, "input-error") as (client, root, gates):
        gates["native-reached"].wait("actual captured owner request write")
        client.send(r="must_not_run <- TRUE", timeout_ms=0)
        gates["native-release"].release()
        client.send(timeout_ms=10000)
        text = last_result_text(client)
        assert "failed to write target setup input: Input/output error" in text, text
        assert "setup cancelled" not in text and "setup interrupted" not in text, text
        _, stderr = client.finish_with_standard_error(expected_exit_status=1)
        assert "failed to write target setup input: Input/output error" in stderr, (
            stderr
        )
        assert [call["args"] for call in calls(root)] == [["version"]]
        assert not (root / "peer/vms").exists()
        return [
            {
                "fault": "setup stdin",
                "control_inferred": False,
                "provider_receipt": False,
            }
        ]


@requires(POSIX, NATIVE_FIXTURES)
def test_output_failure_does_not_infer_control_or_provider_confirmation(
    binary: Path,
) -> list:
    with gated_controller(binary, "read-error") as (client, root, gates):
        gates["native-reached"].wait("provider owner HELLO reached the collector")
        client.send(r="must_not_run <- TRUE", timeout_ms=0)
        gates["native-release"].release()
        client.send(timeout_ms=10000)
        text = last_result_text(client)
        assert "Docker Sandbox output read failed: Input/output error" in text, text
        assert (
            "truncated Docker Sandbox frame payload: failed to fill whole buffer"
            in text
        ), text
        assert (
            "target retirement is unconfirmed (transport exit is not a cleanup barrier)"
            in text
        ), text
        assert "setup cancelled" not in text and "setup interrupted" not in text, text
        client.send(control="restart", r="must_not_run <- TRUE")
        assert (
            "target retirement is unconfirmed (transport exit is not a cleanup barrier)"
            in last_result_text(client)
        )
        client.finish_with_standard_error(expected_exit_status=1)
        assert not (root / "peer/vms").exists()
        assert sum(call["args"][0] == "create" for call in calls(root)) == 1
        assert not (root / "peer/evaluations").exists()
        return [
            {
                "fault": "setup stdout",
                "provider_receipt": False,
                "replacement_launched": False,
            }
        ]


@requires(POSIX)
def test_shutdown_retains_workload_failure_with_valid_provider_cleanup(
    binary: Path,
) -> list:
    with workspace() as root:
        environment = cli_peer(root / "peer")
        configure(root, template=TEMPLATE)
        (root / "peer/mode").write_text("probe-failed-gate")
        with closing(FifoCheckpoint.create(root / "peer/reached")) as reached:
            with McpClient(binary, ("serve",), environment, root) as client:
                client.initialize_and_list_tools()
                reached.wait("workload failure frame written before transport exit")
                _, stderr = client.finish_with_standard_error(expected_exit_status=1)
                assert "probe validation failed" in stderr, stderr
        assert not (root / "peer/vms").exists()
        invoked = [call["args"] for call in calls(root)]
        (created,) = [
            args[args.index("--name") + 1] for args in invoked if args[0] == "create"
        ]
        assert [args for args in invoked if args[0] == "rm"] == [
            ["rm", "--force", created]
        ]
        assert not (root / "peer/evaluations").exists()
        return [
            {
                "workload_error": "probe validation failed",
                "owned_resource_removed": True,
                "shutdown_exit_status": 1,
            }
        ]


@requires(POSIX)
def test_interrupt_during_probe_retains_reason_and_resource_identity(
    binary: Path,
) -> list:
    with workspace() as root:
        environment = cli_peer(root / "peer")
        configure(root, template=TEMPLATE)
        (root / "peer/mode").write_text("probe-gate")
        with closing(FifoCheckpoint.create(root / "peer/reached")) as reached:
            with McpClient(binary, ("serve",), environment, root) as client:
                client.initialize_and_list_tools()
                reached.wait(
                    "probe admitted with captured bootstrap and resource identity"
                )
                client.send(r="must_not_run <- TRUE", timeout_ms=0)
                client.send(control="interrupt", timeout_ms=10000)
                assert "Docker Sandbox setup interrupted" in last_result_text(client)
                client.finish_with_standard_error(expected_exit_status=1)
        invoked = [call["args"] for call in calls(root)]
        (name,) = [
            args[args.index("--name") + 1] for args in invoked if args[0] == "create"
        ]
        assert [args[4] for args in invoked if args[0] == "exec"] == [name]
        assert [args for args in invoked if args[0] == "rm"] == [
            ["rm", "--force", name]
        ]
        assert not (root / "peer/vms").exists()
        assert not (root / "peer/evaluations").exists()
        return [
            {
                "requested_control": "interrupt",
                "captured_resource_removed": True,
                "first_cell_dispatched": False,
            }
        ]


@requires(POSIX)
def test_completed_setup_controls_do_not_abort_the_worker(binary: Path) -> list:
    with workspace() as root:
        environment = cli_peer(root / "peer")
        configure(root, template=TEMPLATE)
        with McpClient(
            binary, ("serve",), environment, root, response_timeout=15
        ) as client:
            client.initialize_and_list_tools()
            client.send(r="42")
            assert last_result_text(client) == "provider peer\n"
            client.send(control="interrupt")
            client.send(control="interrupt")
            client.send(r="43")
            assert last_result_text(client) == "provider peer\n"
            client.finish()
        assert sum(call["args"][0] == "create" for call in calls(root)) == 2
        assert len((root / "peer/interrupts").read_text().splitlines()) == 2
        assert not (root / "peer/vms").exists()
        return [
            {"setup_completed": True, "later_controls": 2, "worker_replaced": False}
        ]


if __name__ == "__main__":
    run_this_suite(__file__)
