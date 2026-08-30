#!/usr/bin/env -S uv run --script

import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TextIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import McpClient, ToolResult, Transcript, code, run_this_suite


PLATFORMS = {"darwin", "linux"}
CAPTURE_NAME = "mcp-console-worker-wire.jsonl"
CAPTURE_STDIN_CLOSE_ENV = "MCP_CONSOLE_MITM_CAPTURE_STDIN_CLOSE"
CAPTURE_WORKER_SIDEBAND_CLOSE_ENV = "MCP_CONSOLE_MITM_CAPTURE_WORKER_SIDEBAND_CLOSE"
SHUTDOWN_ENOTCONN_ENV = "MCP_CONSOLE_MITM_SHUTDOWN_ENOTCONN"


def _tool_text(result: ToolResult) -> str:
    assert result.get("isError") is not True, result
    return result["content"][0]["text"]


class RelayWorkerClient:
    def __init__(
        self,
        binary: Path,
        *,
        capture_stdin_close: bool = False,
        capture_worker_sideband_close: bool = False,
        disable_r_segv_handler: bool = False,
        inject_shutdown_enotconn: bool = False,
    ) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        environment = os.environ.copy()
        environment["TMPDIR"] = str(root)
        environment["MCP_CONSOLE_MITM_WORKER"] = str(binary)
        if capture_stdin_close:
            environment[CAPTURE_STDIN_CLOSE_ENV] = "1"
        if capture_worker_sideband_close:
            environment[CAPTURE_WORKER_SIDEBAND_CLOSE_ENV] = "1"
        if disable_r_segv_handler:
            environment["R_NO_SEGV_HANDLER"] = "1"
        if inject_shutdown_enotconn:
            environment[SHUTDOWN_ENOTCONN_ENV] = "relay"
        mitm = Path(__file__).resolve().parents[2] / "fixtures" / "worker_mitm"
        self._client = McpClient(
            binary,
            ("serve", "--worker", str(mitm)),
            environment,
        )
        self._client._initialize_and_list_tools()

    def send(self, **arguments: object) -> ToolResult:
        return self._client.send(**arguments)

    def _open_capture(self) -> tuple[Path, TextIO]:
        capture = self._capture_path()
        return capture, capture.open(encoding="utf-8")

    def _collect_output(self, output: str, expected_size: int) -> str:
        chunks = [] if output == "[done]" else [output]
        deadline = time.monotonic() + 10
        while sum(map(len, chunks)) < expected_size:
            assert time.monotonic() < deadline, repr("".join(chunks))
            polled = _tool_text(self.send())
            assert polled.endswith("\n[idle]"), repr(polled)
            chunks.append(polled.removesuffix("\n[idle]"))
        output = "".join(chunks)
        assert len(output) == expected_size, repr(output)
        return output

    def _finish(self) -> Transcript:
        transcript = self._read_capture(self._capture_path())
        self._client._finish()
        self._temporary.cleanup()
        return transcript

    def _finish_replacement(
        self,
        old_path: Path,
        old_capture: TextIO,
    ) -> Transcript:
        transcript = self._read_open_capture(old_capture)
        transcript.extend(self._read_capture(self._capture_path(excluding=old_path)))
        old_capture.close()
        self._client._finish()
        self._temporary.cleanup()
        return transcript

    def _capture_path(self, excluding: Path | None = None) -> Path:
        root = Path(self._temporary.name)
        captures = [
            path
            for path in root.glob(f"mcp-console-tmp-*/{CAPTURE_NAME}")
            if path != excluding
        ]
        assert len(captures) == 1, captures
        return captures[0]

    @staticmethod
    def _read_capture(capture: Path) -> Transcript:
        return [
            json.loads(line)
            for line in capture.read_text(encoding="utf-8").splitlines()
        ]

    @staticmethod
    def _read_open_capture(capture: TextIO) -> Transcript:
        return [json.loads(line) for line in capture.read().splitlines()]


def test_restarts_session(binary: Path) -> Transcript:
    client = RelayWorkerClient(
        binary,
        capture_stdin_close=True,
        capture_worker_sideband_close=True,
    )
    # fmt: r
    before_restart = code(r"""
        restart_marker <- "old generation"
        cat("before restart\n")
        """)
    assert _tool_text(client.send(r=before_restart)) == "before restart\n"
    old_path, old_capture = client._open_capture()

    assert _tool_text(client.send(control="restart")) == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
    )
    # fmt: r
    after_restart = code(r"""
        stopifnot(!exists("restart_marker", inherits = FALSE))
        cat("after restart\n")
        """)
    assert _tool_text(client.send(r=after_restart)) == "after restart\n"

    transcript = client._finish_replacement(old_path, old_capture)
    assert {"stdin": {"closed": True}} in transcript
    assert {"worker_sideband": {"closed": True}} in transcript
    return transcript


def test_tolerates_enotconn_during_directional_shutdown(binary: Path) -> Transcript:
    client = RelayWorkerClient(binary, inject_shutdown_enotconn=True)
    assert _tool_text(client.send(r="invisible(NULL)")) == "[done]"
    old_path, old_capture = client._open_capture()
    result = _tool_text(client.send(control="restart"))
    assert result == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
    ), result
    assert _tool_text(client.send(r="cat('replacement ready\\n')")) == (
        "replacement ready\n"
    )
    transcript = client._finish_replacement(old_path, old_capture)
    assert {"shutdown_enotconn": {"direction": "relay"}} in transcript
    return transcript


def test_tolerates_connection_reset_with_unread_shutdown(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    relay = Path(__file__).resolve().parents[2] / "fixtures" / "delayed_sideband_relay"
    interposer_source = (
        Path(__file__).resolve().parents[2] / "fixtures" / "delay_sideband_poll.c"
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        if sys.platform == "darwin":
            interposer = temporary / "reset-sideband-eof.dylib"
            linker_arguments = ["-dynamiclib"]
            linker_libraries = []
        else:
            assert sys.platform == "linux", sys.platform
            interposer = temporary / "reset-sideband-eof.so"
            linker_arguments = ["-shared", "-fPIC"]
            linker_libraries = ["-ldl"]
        subprocess.run(
            [
                "cc",
                *linker_arguments,
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-o",
                interposer,
                interposer_source,
                *linker_libraries,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        reset_marker = temporary / "reset-sideband-eof-injected"
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_RELAY_BINARY"] = str(binary)
        environment["MCP_CONSOLE_TEST_POLL_DYLIB"] = str(interposer)
        environment["MCP_CONSOLE_TEST_POLL_LOADED_NAME"] = "poll-loaded"
        environment["MCP_CONSOLE_TEST_POLL_ARM_NAME"] = "poll-arm"
        environment["MCP_CONSOLE_TEST_POLL_SOCKET_READY_NAME"] = "socket-ready"
        environment["MCP_CONSOLE_TEST_POLL_CANCEL_READY_NAME"] = "cancel-ready"
        environment["MCP_CONSOLE_TEST_RESET_SIDEBAND_EOF"] = str(reset_marker)
        process = subprocess.Popen(
            [relay, zod],
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            start_new_session=True,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None

        def receive() -> dict[str, object]:
            readable, _, _ = select.select([process.stdout], [], [], 10)
            assert readable, "worker relay did not emit an event"
            line = process.stdout.readline()
            assert line, "worker relay closed its event stream"
            event = json.loads(line)
            assert isinstance(event, dict), event
            return event

        def send(message: dict[str, object]) -> None:
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            process.stdin.flush()

        passed = False
        try:
            events = [receive()]
            assert events == [{"kind": "ready"}], events
            send(
                {
                    "kind": "evaluate",
                    "language": "r",
                    "source": "close sideband with unread shutdown",
                }
            )
            events.append(receive())
            assert events[-1] == {
                "kind": "console_output",
                "data": "zod waiting for shutdown\n",
            }, events
            send({"kind": "shutdown", "grace_millis": 5000})

            deadline = time.monotonic() + 10
            while process.poll() is None:
                remaining = deadline - time.monotonic()
                assert remaining > 0, events
                readable, _, _ = select.select([process.stdout], [], [], remaining)
                assert readable, events
                line = process.stdout.readline()
                if line:
                    event = json.loads(line)
                    assert isinstance(event, dict), event
                    events.append(event)
            events.extend(json.loads(line) for line in process.stdout)
            standard_error = process.stderr.read()

            assert reset_marker.exists(), events
            assert process.returncode == 0, standard_error
            assert standard_error == ""
            assert not any(event.get("kind") == "fatal" for event in events), events
            assert events[-2:] == [
                {"kind": "worker_sideband_closed"},
                {"kind": "worker_exited", "code": 0},
            ], events
            passed = True
            return events
        finally:
            if not passed:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()


def test_recovers_after_worker_segfault(binary: Path) -> Transcript:
    # Disable R's fatal-signal UI so the native fault terminates the worker directly.
    client = RelayWorkerClient(
        binary,
        capture_worker_sideband_close=True,
        disable_r_segv_handler=True,
    )
    # fmt: r
    before_crash = code(r"""
        crash_marker <- "old generation"
        cat("before crash\n")
        """)
    assert _tool_text(client.send(r=before_crash)) == "before crash\n"
    old_path, old_capture = client._open_capture()

    # fmt: python
    crash = code(r"""
        import ctypes

        ctypes.string_at(0)
        """)
    result = client.send(python=crash)
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "[worker sideband read failed: worker sideband closed]\n"
        "[worker exited with status 245]\n"
        "[worker stopped: in-memory state lost]\n"
        "[starting new worker]\n"
        "[idle]"
    ), repr(result["content"][0]["text"])

    # fmt: r
    after_crash = code(r"""
        stopifnot(!exists("crash_marker", inherits = FALSE))
        cat("after crash\n")
        """)
    assert _tool_text(client.send(r=after_crash)) == "after crash\n"

    transcript = client._finish_replacement(old_path, old_capture)
    assert {"worker_sideband": {"closed": True}} in transcript
    return transcript


def test_routes_python_output(binary: Path) -> Transcript:
    client = RelayWorkerClient(binary)
    # fmt: r
    r = code(r"""
        suppressWarnings(
          invisible(reticulate::py_run_string("initialized_from_r = True"))
        )
        """)
    output = _tool_text(client.send(r=r))
    assert output == "[done]", repr(output)

    # fmt: python
    python = code(r"""
        import sys

        assert initialized_from_r
        print("Python stdout")
        sys.stderr.write("Python stderr\n")
        raise ValueError("boom")
        """)
    output = _tool_text(client.send(python=python))
    assert output.startswith("Python stdout\nPython stderr\nTraceback"), output
    assert output.endswith("ValueError: boom\n"), output

    # fmt: python
    descendant = code(r"""
        import sys

        print("exec descendant stdout")
        sys.stderr.write("exec descendant stderr\n")
        """)
    # fmt: python
    python = code(rf"""
        import os
        import subprocess
        import sys

        buffer_stdout = sys.stdout.buffer.write(b"buffer stdout\n")
        sys.stdout.buffer.flush()
        buffer_stderr = sys.stderr.buffer.write(b"buffer stderr\n")
        sys.stderr.buffer.flush()
        direct_stdout = os.write(1, b"direct stdout\n")
        direct_stderr = os.write(2, b"direct stderr\n")
        descendant_source = {descendant!r}
        exec_descendant = subprocess.run(
            [sys.executable, "-c", descendant_source],
            check=True,
        )
        """)
    expected = [
        "buffer stdout",
        "buffer stderr",
        "direct stdout",
        "direct stderr",
        "exec descendant stdout",
        "exec descendant stderr",
    ]
    output = _tool_text(client.send(python=python))
    output = client._collect_output(output, sum(len(line) + 1 for line in expected))
    assert sorted(output.splitlines()) == sorted(expected), repr(output)
    return client._finish()


def test_routes_r_console_channels(binary: Path) -> Transcript:
    client = RelayWorkerClient(binary)
    # fmt: r
    r = code(r"""
        cat("R output\n")
        message("R diagnostic")
        utils::file.edit(
          c("/dev/null", "/dev/null"),
          editor = Sys.which("true")
        )
        """)
    assert _tool_text(client.send(r=r)) == (
        "R output\nR diagnostic\nWARNING: Only editing the first in the list of files\n"
    )
    return client._finish()


def test_preserves_python_output_from_fork_children(binary: Path) -> Transcript:
    client = RelayWorkerClient(binary)
    # fmt: r
    r = code(r"""
        python <- Sys.which("python3")
        stopifnot(nzchar(python))
        reticulate::use_python(python, required = TRUE)
        suppressWarnings(invisible(reticulate::py_run_string("fork_ready = True")))
        """)
    assert _tool_text(client.send(r=r)) == "[done]"

    # fmt: python
    python = code(r"""
        import os
        import logging
        import sys
        import tempfile
        import warnings

        assert fork_ready
        logger = logging.Logger("fork child")
        logger.addHandler(logging.StreamHandler())
        redirected = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        parent_stdout = sys.stdout
        sys.stdout = redirected
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            child = os.fork()
        if child == 0:
            print("redirected child stdout", flush=True)
            sys.stderr.write("fork child stderr\n")
            sys.stderr.flush()
            logger.warning("cached child stderr")
            os._exit(0)

        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        sys.stdout = parent_stdout
        redirected.seek(0)
        redirected_capture = redirected.read()
        redirected.close()
        print(f"redirected capture: {redirected_capture!r}")
        parent_stdout = sys.stdout.write("parent stdout\n")
        parent_stderr = sys.stderr.write("parent stderr\n")
        """)
    expected = [
        "redirected capture: 'redirected child stdout\\n'",
        "fork child stderr",
        "cached child stderr",
        "parent stdout",
        "parent stderr",
    ]
    output = _tool_text(client.send(python=python))
    output = client._collect_output(output, sum(len(line) + 1 for line in expected))
    assert sorted(output.splitlines()) == sorted(expected), repr(output)
    return client._finish()


def test_drains_standard_streams_while_evaluating(binary: Path) -> Transcript:
    client = RelayWorkerClient(binary)
    size = 4 * 1024 * 1024
    # fmt: python
    python = code(rf"""
        import os


        def write_all(file_descriptor, data):
            data = memoryview(data)
            while data:
                data = data[os.write(file_descriptor, data) :]


        write_all(1, b"x" * {size})
        write_all(2, b"y" * {size})
        """)
    output = _tool_text(client.send(python=python))
    output = client._collect_output(output, 2 * size)
    assert output.count("x") == size
    assert output.count("y") == size

    transcript = client._finish()
    assert transcript[-2] == {"stdout": "x" * size, "stderr": "y" * size}
    assert transcript[-1] == {"worker": {"kind": "completed"}}
    transcript[-2]["stdout"] = f"<{size} bytes>"
    transcript[-2]["stderr"] = f"<{size} bytes>"
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
