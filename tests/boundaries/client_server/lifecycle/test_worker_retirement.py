#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
import time
from contextlib import ExitStack, closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.checkpoints import FifoCheckpoint, wait_for_worker_file
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, POSIX, UNPRIVILEGED, requires


@requires(POSIX, NATIVE_FIXTURES)
@executions(DIRECT, SANDBOXED)
def test_restart_preserves_overlapping_failed_retirement(
    binary: Path, execution: Execution
) -> Transcript:
    # fmt: python
    launcher = code("""
        import os
        import sys

        os.environ["MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_PID"] = str(os.getpid())
        os.environ[os.environ.pop("MCP_CONSOLE_TEST_LOADER")] = os.environ.pop(
            "MCP_CONSOLE_TEST_RETIREMENT_LIBRARY"
        )
        os.execv(sys.argv[1], sys.argv[1:])
        """)
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
        root = Path(temporary)
        blocked, release, returned = [
            resources.enter_context(closing(FifoCheckpoint.create(root / name)))
            for name in (
                "blocked",
                "release",
                "returned",
            )
        ]
        environment = {
            **os.environ,
            "TMPDIR": str(root),
            "MCP_CONSOLE_TEST_LOADER": LOADER_VARIABLE,
            "MCP_CONSOLE_TEST_RETIREMENT_LIBRARY": str(
                build_interposer(root, "launcher_retirement_interposer")
            ),
            "MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_BLOCKED": str(blocked.path),
            "MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_RELEASE": str(release.path),
            "MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_RETURNED": str(returned.path),
            "MCP_CONSOLE_TEST_GENERATION_FAILED": str(root / "failed"),
        }
        relay = (
            Path(__file__).resolve().parents[3]
            / "fixtures/server_relay/failed_retirement_restart.py"
        )
        client = resources.enter_context(
            McpClient(
                Path(sys.executable),
                (
                    "-c",
                    launcher,
                    str(binary),
                    *execution.serve("--worker", str(binary), "--relay", str(relay)),
                ),
                environment,
                root,
            )
        )
        resources.callback(release.release)
        client.initialize_and_list_tools()
        failed = client.start_send(r="42")
        checkpoint = wait_for_worker_file(root, "retirement-fault-sent", client)
        fault_release, fault_sent = [
            resources.enter_context(
                closing(FifoCheckpoint.attach(checkpoint.with_name(name)))
            )
            for name in ("retirement-fault-release", "retirement-fault-sent")
        ]
        resources.callback(fault_release.release)
        blocked.wait(
            "failed-worker retirement reached native cleanup after the dispatcher barrier"
        )
        (root / "failed").touch()
        restart = client.start_send(control="restart")
        # Public admission is the generation-change receipt. Intermediate
        # probes can see the still-active cell or the reserved control; retain
        # only the final receipt in the transcript after asserting each probe.
        first_probe = len(client.transcript)
        deadline = time.monotonic() + 10
        while True:
            probe = client.send(r="must not run during retirement", timeout_ms=0)
            text = last_result_text(client)
            assert probe["isError"], probe
            if text == "[worker is restarting]":
                break
            assert text in (
                "[session control is in progress]",
                "[worker is already evaluating a cell; poll without a code field]",
            ), probe
            assert time.monotonic() < deadline, client.transcript
        client.transcript[first_probe:] = client.transcript[-1:]
        fault_release.release()
        fault_sent.wait("old relay published Fatal during retirement")
        release.release()
        returned.wait("native retirement signal completed")
        client.receive_many([failed, restart])
        next_cell = client.send(r="42")
        assert failed["result"]["isError"], failed
        assert (
            "scripted retirement failure" in failed["result"]["content"][0]["text"]
        ), failed
        assert restart["result"]["isError"], (failed, restart, next_cell)
        restart_text = restart["result"]["content"][0]["text"]
        assert "scripted retirement failure" in restart_text, restart
        assert "[starting new worker]" not in restart_text, restart
        assert next_cell["isError"], next_cell
        assert last_result_text(client) == "[worker is shutting down]", next_cell
        transcript, stderr = client.finish_with_standard_error(expected_exit_status=1)
        assert stderr == "scripted retirement failure\n", stderr
        return transcript + [{"exit_status": 1, "stderr": stderr}]


@requires(POSIX, NATIVE_FIXTURES, UNPRIVILEGED)
def test_failed_storage_retirement_blocks_replacement(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
        root = Path(directory)
        owned = root / "owned"
        owned.mkdir()
        reaped, shutdown, retired, relay_release, signal_release = [
            resources.enter_context(closing(FifoCheckpoint.create(root / name)))
            for name in (
                "worker-reaped",
                "shutdown-written",
                "launcher-retiring",
                "relay-release",
                "signal-release",
            )
        ]
        environment = dict(
            os.environ,
            PATH=str(root),
            RETICULATE_PYTHON=sys.executable,
            TMPDIR=str(owned),
            MCP_CONSOLE_TEST_STORAGE_RETIREMENT_ROOT=str(root),
        )
        environment[LOADER_VARIABLE] = str(build_interposer(root, "storage_retirement"))
        environment.pop("R_HOME", None)
        temporary = None
        try:
            with (
                McpClient(binary, DIRECT.serve(), environment, root) as client,
                ExitStack() as gates,
            ):
                gates.callback(relay_release.release)
                gates.callback(signal_release.release)
                client.initialize_and_list_tools()
                # fmt: python
                python = code("""
                    import os
                    from pathlib import Path

                    temporary = Path(os.environ["TMPDIR"])
                    Path("worker-temporary").write_text(str(temporary))
                    restricted = temporary / "restricted"
                    restricted.mkdir()
                    (restricted / "retained.txt").write_text("private contents")
                    restricted.chmod(0)
                    """)
                client.send(python=python)
                temporary = Path((root / "worker-temporary").read_text())
                assert temporary.is_relative_to(owned), temporary
                failed = client.start_send(python="os._exit(47)")
                reaped.wait("real relay reaped the worker that exited with status 47")
                shutdown.wait("server wrote the generation's complete Shutdown command")
                retired.wait(
                    "server reached launcher retirement after its dispatcher barrier"
                )
                relay_release.release()
                signal_release.release()
                client.receive(failed)
                failure = failed["result"]
                assert failure["isError"], failure
                assert "cannot remove worker temporary directory" in last_result_text(
                    client
                )
                replacement = client.send(python="print('replacement ran')")
                assert replacement["isError"], replacement
                assert "worker is shutting down" in last_result_text(client)
                assert "replacement ran\n" not in last_result_text(client)
                transcript, stderr = client.finish_with_standard_error(
                    expected_exit_status=1
                )
                assert "cannot remove worker temporary directory" in stderr, stderr
                for record in transcript:
                    for content in record.get("result", {}).get("content", []):
                        if content["type"] == "text":
                            content["text"] = content["text"].replace(
                                str(temporary), "<worker temporary>"
                            )
                return transcript + [
                    {
                        "exit_status": 1,
                        "stderr": stderr.replace(str(temporary), "<worker temporary>"),
                    }
                ]
        finally:
            if temporary is not None:
                (temporary / "restricted").chmod(0o700)


@requires(POSIX, NATIVE_FIXTURES)
@executions(DIRECT, SANDBOXED)
def test_eof_preserves_completed_failed_retirement(
    binary: Path, execution: Execution
) -> Transcript:
    return _retirement_failure_at_eof(binary, execution, close_during_retirement=False)


@requires(POSIX, NATIVE_FIXTURES)
@executions(DIRECT, SANDBOXED)
def test_eof_preserves_inflight_failed_retirement(
    binary: Path, execution: Execution
) -> Transcript:
    return _retirement_failure_at_eof(binary, execution, close_during_retirement=True)


def _retirement_failure_at_eof(
    binary: Path, execution: Execution, *, close_during_retirement: bool
) -> Transcript:
    # fmt: python
    launcher = code("""
        import os
        import sys

        os.environ["MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_PID"] = str(os.getpid())
        os.environ[os.environ.pop("MCP_CONSOLE_TEST_LOADER")] = os.environ.pop(
            "MCP_CONSOLE_TEST_RETIREMENT_LIBRARY"
        )
        os.execv(sys.argv[1], sys.argv[1:])
        """)
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
        root = Path(temporary)
        blocked, release, returned = [
            resources.enter_context(closing(FifoCheckpoint.create(root / name)))
            for name in ("blocked", "release", "returned")
        ]
        environment = {
            **os.environ,
            "TMPDIR": str(root),
            "MCP_CONSOLE_TEST_LOADER": LOADER_VARIABLE,
            "MCP_CONSOLE_TEST_RETIREMENT_LIBRARY": str(
                build_interposer(root, "launcher_retirement_interposer")
            ),
            "MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_BLOCKED": str(blocked.path),
            "MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_RELEASE": str(release.path),
            "MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_RETURNED": str(returned.path),
            "MCP_CONSOLE_TEST_GENERATION_FAILED": str(root / "failed"),
        }
        relay = (
            Path(__file__).resolve().parents[3]
            / "fixtures/server_relay/failed_retirement_restart.py"
        )
        client = resources.enter_context(
            McpClient(
                Path(sys.executable),
                (
                    "-c",
                    launcher,
                    str(binary),
                    *execution.serve("--worker", str(binary), "--relay", str(relay)),
                ),
                environment,
                root,
            )
        )
        resources.callback(release.release)
        client.initialize_and_list_tools()
        failed = client.start_send(r="42")
        checkpoint = wait_for_worker_file(root, "retirement-fault-sent", client)
        fault_release, fault_sent = [
            resources.enter_context(
                closing(FifoCheckpoint.attach(checkpoint.with_name(name)))
            )
            for name in ("retirement-fault-release", "retirement-fault-sent")
        ]
        resources.callback(fault_release.release)
        blocked.wait("failed-worker retirement crossed the dispatcher barrier")
        if close_during_retirement:
            client.stdin.close()
        fault_release.release()
        fault_sent.wait("relay published Fatal after the retirement barrier")
        release.release()
        returned.wait("native retirement signal completed")
        # Receiving this response proves failed-worker retirement, including
        # dispatcher/I/O joins, finished before EOF in the completed schedule.
        client.receive(failed)
        failure = failed["result"]
        assert failure["isError"], failure
        assert "scripted retirement failure" in last_result_text(client), failure
        if not close_during_retirement:
            rejected = client.send(r="42")
            assert rejected["isError"], rejected
            assert last_result_text(client) == "[worker is shutting down]", rejected
        transcript, stderr = client.finish_with_standard_error(expected_exit_status=1)
        assert stderr == "scripted retirement failure\n", stderr
        return transcript + [{"exit_status": 1, "stderr": stderr}]
