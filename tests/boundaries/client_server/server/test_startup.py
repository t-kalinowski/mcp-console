#!/usr/bin/env -S uv run --script

import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.requirements import POSIX, R, requires
from support.checkpoints import FifoCheckpoint
from support.assertions import last_result_text
from support.client import McpClient
from support.normalization import code
from support.records import Transcript
from support.r import r_test_environment
from support.resolvers import bare_runtime_environment
from support.suites import run_this_suite


@contextmanager
def discovery_environment(
    *, r_home: Path | None = None
) -> Iterator[tuple[dict[str, str], FifoCheckpoint, FifoCheckpoint, int]]:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        reached = FifoCheckpoint.create(root / "reached")
        release = FifoCheckpoint.create(root / "release")
        alive = root / "alive"
        os.mkfifo(alive)
        alive_reader = os.open(alive, os.O_RDONLY | os.O_NONBLOCK)
        probe = root / "R"
        # The blocked descendant must exist before discovery reports readiness.
        # A shell spawning dd after the checkpoint can race cancellation.
        probe.write_text(
            code(f"""
                #!{sys.executable} -S
                import os
                import sys
                from pathlib import Path

                home = {str(r_home) if r_home is not None else None!r}
                with Path({str(alive)!r}).open("wb", buffering=0) as lifetime:
                    child = os.fork()
                    if child == 0:
                        assert lifetime.write(b"1") == 1
                        with Path({str(reached.path)!r}).open("wb", buffering=0) as reached:
                            assert reached.write(b"1") == 1
                        with Path({str(release.path)!r}).open("rb", buffering=0) as release:
                            assert release.read(1) == b"1"
                        os._exit(0)
                    assert os.waitpid(child, 0) == (child, 0)
                    if home is None:
                        print("fixture R discovery failed", file=sys.stderr)
                        raise SystemExit(17)
                    print(home)
                """)
        )
        probe.chmod(0o755)
        # The selected R launcher uses sh to report its resource directories.
        (root / "sh").symlink_to(shutil.which("sh"))
        environment = bare_runtime_environment(os.environ.copy(), root / "library")
        environment["R_PROFILE_USER"] = os.devnull
        environment.pop("R_HOME", None)
        environment["PATH"] = str(root)
        try:
            yield environment, reached, release, alive_reader
            assert select.select([alive_reader], [], [], 5)[0], (
                "discovery survived MCP closure"
            )
            assert os.read(alive_reader, 1) == b"", "probe lifetime pipe must close"
        finally:
            os.close(alive_reader)
            release.close()
            reached.close()


@contextmanager
def gated_discovery(
    binary: Path, *, r_home: Path | None = None
) -> Iterator[tuple[McpClient, FifoCheckpoint]]:
    with discovery_environment(r_home=r_home) as (environment, reached, release, alive):
        with McpClient(
            binary, ("serve", "--no-sandbox"), environment, response_timeout=5
        ) as client:
            reached.wait("runtime discovery is blocked")
            assert os.read(alive, 1) == b"1"
            yield client, release


@requires(POSIX)
def test_closed_input_cancels_discovery_with_blocked_stdout(binary: Path) -> Transcript:
    with discovery_environment() as (environment, reached, release, alive):
        read_output, write_output = os.pipe()
        os.set_blocking(write_output, False)
        try:
            while True:
                os.write(write_output, b"x" * 4096)
        except BlockingIOError:
            pass
        os.set_blocking(write_output, True)
        try:
            with tempfile.TemporaryDirectory() as directory:
                environment["MCP_CONSOLE_HOME"] = directory
                process = subprocess.Popen(
                    [binary, "serve", "--no-sandbox"],
                    env=environment,
                    cwd=directory,
                    stdin=subprocess.PIPE,
                    stdout=write_output,
                    stderr=subprocess.PIPE,
                )
                assert process.stdin is not None and process.stderr is not None
                try:
                    reached.wait("runtime discovery is blocked")
                    assert os.read(alive, 1) == b"1"
                    process.stdin.write(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "initialize",
                                "params": {
                                    "protocolVersion": "2025-03-26",
                                    "capabilities": {},
                                    "clientInfo": {
                                        "name": "startup-test",
                                        "version": "1",
                                    },
                                },
                            }
                        ).encode()
                        + b"\n"
                    )
                    process.stdin.close()
                    assert process.wait(timeout=5) != 0
                    assert (
                        b"server startup cancelled because MCP input closed"
                        in process.stderr.read()
                    )
                finally:
                    if process.poll() is None:
                        release.release()
                        process.kill()
                        process.wait(timeout=5)
                    process.stderr.close()
        finally:
            os.close(write_output)
            os.close(read_output)
    return [{"closed_input_retired_discovery_with_blocked_stdout": True}]


@requires(POSIX)
def test_closed_input_before_handshake_reports_cancelled_preparation(
    binary: Path,
) -> Transcript:
    with gated_discovery(binary) as (client, _):
        client.stdin.close()
        assert client.process.wait(timeout=5) != 0
        assert client.stdout.read() == ""
        errors = client.stderr.read()
        assert errors == "server startup cancelled because MCP input closed\n", errors
        return [{"stderr": errors}]


@requires(POSIX)
def test_initializes_while_runtime_discovery_is_blocked(binary: Path) -> Transcript:
    with gated_discovery(binary) as (client, _):
        client.initialize_and_list_tools()
        client.request("ping")
        return client.finish()


@requires(POSIX)
def test_reports_discovery_failure_without_losing_mcp(binary: Path) -> Transcript:
    with gated_discovery(binary) as (client, release):
        client.initialize_and_list_tools()
        pending = client.start_send(r="stop('must not execute')")
        ping = client.start_request("ping")
        client.receive(ping)
        release.release()
        client.receive(pending)
        assert "fixture R discovery failed" in str(pending), pending
        assert pending["result"]["isError"] is True, pending
        assert client.send(r="stop('must not retry discovery')") == pending["result"]
        inspected = client.send(requirements={"action": "get"})
        assert inspected["isError"] is True, inspected
        assert "fixture R discovery failed" in str(inspected), inspected
        assert client.request("tools/list")["result"] == client.transcript[2]["result"]
        transcript, errors = client.finish_with_standard_error(expected_exit_status=1)
        assert "fixture R discovery failed" in errors, errors
        return transcript + [{"stderr": errors}]


@requires(POSIX)
def test_failed_handshake_cancels_discovery_with_input_open(binary: Path) -> Transcript:
    with gated_discovery(binary) as (client, _):
        client.start_request("tools/list")
        assert client.process.wait(timeout=5) != 0
        errors = client.stderr.read()
        assert errors, "invalid handshake must report its failure"
        return [{"stderr": errors, "input_remained_open": not client.stdin.closed}]


@requires(POSIX)
def test_bounds_discovery_failure_during_requirement_inspection(
    binary: Path,
) -> Transcript:
    with discovery_environment() as (environment, reached, release, alive):
        probe = reached.path.parent / "R"
        probe.write_text(
            probe.read_text().replace(
                "fixture R discovery failed",
                "fixture R discovery failed " + "x" * (32 * 1024),
            )
        )
        with McpClient(binary, ("serve", "--no-sandbox"), environment) as client:
            reached.wait("runtime discovery is blocked")
            assert os.read(alive, 1) == b"1"
            client.initialize_and_list_tools()
            release.release()
            failure = client.send(requirements={"action": "get"})
            assert failure["isError"] is True, failure
            content = failure["content"][0]["text"]
            assert "fixture R discovery failed" in content, failure
            assert len(content.encode()) <= 8192, len(content.encode())
            _, stderr = client.finish_with_standard_error(expected_exit_status=1)
            assert "fixture R discovery failed" in stderr
            return [{"inspection_error": "bounded to 8 KiB", "isError": True}]


@requires(POSIX)
def test_cancelled_send_does_not_cancel_shared_discovery(binary: Path) -> Transcript:
    with gated_discovery(binary) as (client, release):
        client.initialize_and_list_tools()
        pending = client.start_send(r="stop('cancelled request must not execute')")
        wait_for_send_admission(client)
        client.send(r="stop('another cell must not execute')", timeout_ms=0)
        assert "already evaluating" in str(client.transcript[-1]["result"])
        client.notify("notifications/cancelled", requestId=pending["id"])
        client.request("ping")
        release.release()
        result = client.send()
        assert result["isError"], result
        assert "fixture R discovery failed" in str(result), result
        transcript, errors = client.finish_with_standard_error(expected_exit_status=1)
        assert "fixture R discovery failed" in errors, errors
        return transcript + [{"stderr": errors}]


def wait_for_send_admission(client: McpClient) -> None:
    # Writing a request does not mean its handler has reserved the cell yet.
    # Discovery is gated, so an empty observation can only report startup or
    # the pending send's exclusive wait claim. Never submit a competing cell
    # until that public receipt proves admission.
    deadline = time.monotonic() + 30
    first_poll = len(client.transcript)
    while True:
        result = client.send(timeout_ms=0)
        if result.get("isError"):
            assert result == {
                "content": [
                    {
                        "type": "text",
                        "text": "[worker evaluation is already being polled]",
                    }
                ],
                "isError": True,
            }, result
            break
        assert result["content"] == [{"type": "text", "text": "[worker starting]"}], (
            result
        )
        assert time.monotonic() < deadline, "pending send did not claim its evaluation"
    # Only the scheduling-dependent startup observations are incidental.
    client.transcript[first_poll:] = [client.transcript[-1]]


@requires(POSIX)
@requires(R)
def test_queued_r_cell_executes_once_after_discovery(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    with (
        gated_discovery(binary, r_home=Path(environment["R_HOME"])) as (
            client,
            release,
        ),
        closing(
            FifoCheckpoint.create(Path(client.temporary_directory.name) / "evaluated")
        ) as evaluated,
    ):
        client.initialize_and_list_tools()
        pending = client.start_send(
            # fmt: r
            r=code("""
                started <- get0("started", ifnotfound = 0L) + 1L
                checkpoint <- fifo("evaluated", "wb", blocking = TRUE)
                writeBin(charToRaw("1"), checkpoint)
                close(checkpoint)
                started
                """),
            timeout_ms=0,
        )
        client.receive(pending)
        assert pending["result"]["content"] == [
            {"type": "text", "text": "\n[running; poll with an empty send]"}
        ], pending
        client.request("ping")
        release.release()
        evaluated.wait("the accepted R cell executed", timeout=600)
        client.send(timeout_ms=10_000)
        assert last_result_text(client) == "[1] 1\n", client.transcript[-1]
        client.send(r="started")
        assert last_result_text(client) == "[1] 1\n", last_result_text(client)
        return client.finish()


@requires(POSIX)
@requires(R)
def test_cancelled_wait_preserves_admitted_cell_after_discovery(
    binary: Path,
) -> Transcript:
    environment, _ = r_test_environment()
    with gated_discovery(binary, r_home=Path(environment["R_HOME"])) as (
        client,
        release,
    ):
        client.initialize_and_list_tools()
        pending = client.start_send(r="cancelled_cell_ran <- TRUE")
        wait_for_send_admission(client)
        client.send(r="stop('another cell must not execute')", timeout_ms=0)
        assert "already evaluating" in str(client.transcript[-1]["result"])
        client.notify("notifications/cancelled", requestId=pending["id"])
        client.request("ping")
        release.release()
        # Releasing discovery still leaves cold R startup and the admitted
        # cell to finish. Keep the raw cancellation/poll exchange intact.
        client.response_timeout = 600
        try:
            client.send(timeout_ms=600_000)
        finally:
            client.response_timeout = 5
        assert last_result_text(client) == "[done]", client.transcript[-1]
        client.send(r='exists("cancelled_cell_ran", inherits = FALSE)')
        assert last_result_text(client) == "[1] TRUE\n", last_result_text(client)
        return client.finish()


def test_first_send_uses_background_runtime(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        environment = os.environ.copy()
        environment.pop("R_HOME", None)
        environment["PATH"] = temporary
        with McpClient(
            binary,
            ("serve", "--no-sandbox", "-c", "python=" + json.dumps(sys.executable)),
            environment,
        ) as client:
            client.initialize_and_list_tools()
            client.send(python="started = globals().get('started', 0) + 1\nstarted")
            assert last_result_text(client) == "1\n", last_result_text(client)
            client.send(python="started")
            assert last_result_text(client) == "1\n", last_result_text(client)
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
