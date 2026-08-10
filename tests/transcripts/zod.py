#!/usr/bin/env -S uv run --script

import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from _support import McpClient, Transcript, code, run_this_suite


PLATFORMS = {"darwin"}
LARGE_OUTPUT_SIZE = 2 * 1024 * 1024
PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42Y"
    "AAAAASUVORK5CYII="
)


def test_routes_send_over_sideband(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()
    client.send(r="echo")
    client.send(python="echo")
    client.send(sql="echo")
    return client._finish()


def test_returns_worker_images(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()
    client.send(r="emit image")
    result = client.transcript[-1]["result"]
    assert result == {
        "content": [
            {"type": "text", "text": "before image\n"},
            {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
            {"type": "text", "text": "after image\n"},
        ],
        "isError": False,
    }, result
    return client._finish()


def test_custom_worker_skips_managed_python_preflight(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    environment["R_HOME"] = "/mcp-console-custom-worker-must-not-run-rscript"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
        environment,
    )
    client._initialize_and_list_tools()
    # fmt: python
    python = code(r"""
        echo
        """).removesuffix("\n")
    client.send(python=python)
    result = client.session(
        action="restart",
        requirements={"python": ["py-yaml12"]},
    )
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "Python requirements are unavailable with a custom worker"
    )
    client.send(r="echo")
    assert last_tool_text(client) == "zod: echo\n"
    return client._finish()


def test_captures_worker_stdout(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()
    client.send(r="emit stdout")
    output = last_tool_text(client)
    assert_large_output(output, "zod stdout 👩🏽‍💻\n")
    client.transcript[-1]["result"]["content"][0]["text"] = (
        "zod stdout 👩🏽‍💻\n<large output>\n"
    )
    return client._finish()


def test_drains_background_stderr_while_idle(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client._initialize_and_list_tools()
        client.send(r="start background stderr")
        assert last_tool_text(client) == "[done]"
        started = wait_for_marker(
            temporary_path,
            "zod-background-stderr-started",
            client,
        )
        (started.parent / "zod-release-background-stderr").touch()
        wait_for_marker(
            temporary_path,
            "zod-background-stderr-emitted",
            client,
        )

        client.send(timeout_ms=0)
        output = last_tool_text(client)
        assert output.endswith("\n[idle]"), output[-100:]
        assert_large_output(
            output.removesuffix("\n[idle]"),
            "zod background stderr\n",
        )
        client.transcript[-1]["result"]["content"][0]["text"] = (
            "zod background stderr\n<large output>\n[idle]"
        )
        return client._finish()


def test_times_out_and_polls_running_evaluation(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()
    client.send(r="echo")
    client.send(
        r="complete after timeout",
        timeout_ms=10,
    )
    output = client.transcript[-1]["result"]["content"][0]["text"]
    assert output == "\n[running]", output
    client.send(timeout_ms=3_000)
    output = client.transcript[-1]["result"]["content"][0]["text"]
    assert output == "zod: complete after timeout\n", output
    client.send(r="echo")
    return client._finish()


def test_accepts_idle_stdin(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    client.send(stdin="cold\n")
    assert last_tool_text(client) == "\n[idle]"
    client.send(r="input without request")
    assert last_tool_text(client) == "zod stdin: cold\n"

    client.send(stdin="idle\n")
    assert last_tool_text(client) == "\n[idle]"
    client.send(r="input without request")
    assert last_tool_text(client) == "zod stdin: idle\n"
    return client._finish()


def test_routes_combined_and_followup_stdin(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()

    client.send(
        r="input length without request",
        stdin=("x" * 1024) + "café\0\n",
    )
    client.transcript[-1]["send"]["stdin"] = "<long UTF-8 stdin containing NUL>"
    assert last_tool_text(client) == "zod stdin length: 1030\n"

    client.send(r="input without request", timeout_ms=0)
    assert last_tool_text(client) == "\n[running]"
    client.send(stdin="followup\n", timeout_ms=3_000)
    assert last_tool_text(client) == "zod stdin: followup\n"

    client.send(r="request input")
    assert last_tool_text(client) == '[input requested: "zod> "]\n[stdin needed]'
    client.send(stdin="")
    assert last_tool_text(client) == "\n[stdin needed]"
    client.send(stdin="prompted\n")
    assert last_tool_text(client) == "zod stdin: prompted\n"

    client.send(
        r="input without request then request input",
        stdin="first\n",
        timeout_ms=1_000,
    )
    assert last_tool_text(client) == '[input requested: "second> "]\n[stdin needed]'
    client.send(stdin="second\n")
    assert last_tool_text(client) == "zod stdin: first|second\n"

    client.send(r="echo", stdin="stale\n")
    assert last_tool_text(client) == "zod: echo\n"
    client.send(r="input without request")
    assert last_tool_text(client) == "zod stdin: stale\n"

    client.send(r="echo", stdin="x" * (128 * 1024), timeout_ms=1_000)
    client.transcript[-1]["send"]["stdin"] = "<large unread stdin>"
    assert last_tool_text(client) == "zod: echo\n"
    return client._finish()


def test_preserves_unexposed_input_output(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client._initialize_and_list_tools()

        client.send(
            r="request input after timeout",
            stdin="answer\n",
            timeout_ms=0,
        )
        assert last_tool_text(client) == "\n[running]"
        waiting = wait_for_marker(
            temporary_path,
            "zod-waiting-to-request-input",
            client,
        )
        (waiting.parent / "zod-release-input-request").touch()
        wait_for_marker(temporary_path, "zod-input-received", client)

        client.send(timeout_ms=3_000)
        assert last_tool_text(client) == (
            'before\n[input requested: "late> "]\nduring request\nzod stdin: answer\n'
        )
        return client._finish()


def last_tool_text(client: McpClient) -> str:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    return result["content"][0]["text"]


def assert_large_output(output: str, prefix: str) -> None:
    expected = prefix + ("x" * LARGE_OUTPUT_SIZE)
    assert output.startswith(expected), (
        f"captured {len(output)} bytes without the complete {len(expected)}-byte payload"
    )
    barrier = output.removeprefix(expected)
    assert barrier and not barrier.strip("y"), "unexpected text after captured payload"


def test_restarts_after_unexpected_sideband_message(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_REPORT_PROCESS_GROUP"] = "1"
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        worker_group = None
        passed = False
        try:
            client._initialize_and_list_tools()
            failed_call = client._start_send(r="violate protocol")
            group_marker = wait_for_marker(
                temporary_path,
                "zod-process-group",
                client,
            )
            worker_group = read_worker_group(group_marker)
            client._receive(failed_call)
            result = failed_call["result"]
            assert result["isError"] is True
            assert result["content"][0]["text"] == (
                "zod output before protocol failure\n"
                "[worker sent an unexpected ready message]"
            )
            assert not process_group_exists(worker_group), "Zod outlived its failure"

            restarted_call = client._start_send(r="complete silently")
            client._receive(restarted_call)
            assert last_tool_text(client) == (
                "\n[worker restarted: in-memory state lost]\n"
            )
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_group(worker_group)
                stop_process(client.process)


def test_restarts_after_worker_exit(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()
    client.send(r="exit unexpectedly")
    assert client.transcript[-1]["result"]["isError"] is True
    client.send(stdin="replacement\n")
    assert last_tool_text(client) == (
        "\n[worker restarted: in-memory state lost]\n[idle]"
    )
    client.send(r="input without request")
    assert last_tool_text(client) == "zod stdin: replacement\n"
    return client._finish()


def test_explicit_restart_preserves_pending_restart_notice(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()
    client.send(r="exit unexpectedly")
    assert client.transcript[-1]["result"]["isError"] is True

    client.session(action="restart")
    assert last_tool_text(client) == "[restarted]"

    client.send(r="echo")
    assert last_tool_text(client) == (
        "zod: echo\n[worker restarted: in-memory state lost]\n"
    )
    client.send(r="echo")
    assert last_tool_text(client) == "zod: echo\n"
    return client._finish()


def test_restart_closes_worker_stdin(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client._initialize_and_list_tools()
        client.send(r="wait for stdin close", timeout_ms=0)
        assert last_tool_text(client) == "\n[running]"
        wait_for_marker(
            temporary_path,
            "zod-waiting-for-stdin-close",
            client,
        )

        client.session(action="restart")
        assert last_tool_text(client) == "[restarted]"

        client.send(r="echo")
        output = last_tool_text(client)
        prefix = "zod stdin closed\n" + ("x" * LARGE_OUTPUT_SIZE)
        assert output.startswith(prefix), "worker stdin did not close before restart"
        assert output.endswith("zod: echo\n")
        barrier = output.removeprefix(prefix).removesuffix("zod: echo\n")
        assert barrier and not barrier.strip("y"), "unexpected old-worker output"
        client.transcript[-1]["result"]["content"][0]["text"] = (
            "zod stdin closed\n<large output>\nzod: echo\n"
        )
        return client._finish()


def test_restart_force_stops_stalled_worker(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_REPORT_PROCESS_GROUP"] = "1"
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        worker_group = None
        passed = False
        try:
            client._initialize_and_list_tools()
            client.send(r="stall", timeout_ms=0)
            assert last_tool_text(client) == "\n[running]"
            group_marker = wait_for_marker(
                temporary_path,
                "zod-process-group",
                client,
            )
            worker_group = read_worker_group(group_marker)
            wait_for_marker(temporary_path, "zod-stalled", client)

            restart_call = client._start_session(action="restart")
            wait_for_process_group_exit(worker_group, client)
            client._receive(restart_call)
            assert last_tool_text(client) == "[restarted]"

            client.send(r="echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_group(worker_group)
                stop_process(client.process)


def test_restart_starts_first_worker_and_waits_until_ready(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        startup_control = temporary_path / "zod-startup-control"
        startup_release = temporary_path / "zod-startup-release"
        startup_control.write_text("block", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        environment["ZOD_STARTUP_RELEASE"] = str(startup_release)
        environment["ZOD_REPORT_PROCESS_GROUP"] = "1"
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        worker_group = None
        passed = False
        try:
            client._initialize_and_list_tools()
            restarted = client._start_session(action="restart")
            wait_for_marker(
                temporary_path,
                "zod-replacement-waiting-ready",
                client,
            )
            worker_group = read_worker_group(
                wait_for_marker(temporary_path, "zod-process-group", client)
            )

            while_restarting = client._start_send(r="echo")
            client._receive(while_restarting)
            result = while_restarting["result"]
            assert result["isError"] is True
            assert result["content"][0]["text"] == "worker is restarting"

            startup_release.touch()
            client._receive(restarted)
            assert restarted["result"]["content"][0]["text"] == "[restarted]"

            after_restart = client._start_send(r="echo")
            client._receive(after_restart)
            assert after_restart["result"]["content"][0]["text"] == "zod: echo\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_group(worker_group)
                stop_process(client.process)


def test_restart_discards_unread_stdin(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    client = McpClient(
        binary,
        ("serve", "--worker", str(zod)),
    )
    client._initialize_and_list_tools()
    client.send(stdin="stale\n")
    assert last_tool_text(client) == "\n[idle]"

    client.session(action="restart")
    assert last_tool_text(client) == "[restarted]"

    client.send(r="input without request", stdin="fresh\n")
    assert last_tool_text(client) == "zod stdin: fresh\n"
    return client._finish()


def test_reports_restart_notice_on_next_response(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        startup_control = temporary_path / "zod-startup-control"
        startup_release = temporary_path / "zod-startup-release"
        startup_control.write_text("ready", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        environment["ZOD_STARTUP_RELEASE"] = str(startup_release)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client._initialize_and_list_tools()
        client.send(r="exit unexpectedly")
        assert client.transcript[-1]["result"]["isError"] is True

        startup_control.write_text("block", encoding="utf-8")
        client.send(r="complete after release", timeout_ms=0)
        assert last_tool_text(client) == "\n[running]"
        wait_for_marker(temporary_path, "zod-replacement-waiting-ready", client)
        startup_release.touch()
        evaluation_started = wait_for_marker(
            temporary_path,
            "zod-evaluation-started",
            client,
        )

        client.send(r="echo")
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == (
            "[worker is already evaluating a cell; poll without a code field]"
            "\n[worker restarted: in-memory state lost]\n"
        )

        (evaluation_started.parent / "zod-release-evaluation").touch()
        client.send(timeout_ms=3_000)
        output = last_tool_text(client)
        assert output == "zod: complete after release\n", repr(output)
        client.send(r="echo")
        assert last_tool_text(client) == "zod: echo\n"
        return client._finish()


def test_retries_initial_startup_silently(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        startup_control = Path(temporary_directory) / "zod-startup-control"
        startup_control.write_text("fail", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client._initialize_and_list_tools()
        client.send(r="echo")
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == (
            "worker sideband read failed: worker sideband closed"
        )
        startup_control.write_text("ready", encoding="utf-8")
        client.send(r="echo")
        assert last_tool_text(client) == "zod: echo\n"
        return client._finish()


def test_runs_worker_inside_sandbox(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        host_file = Path(temporary_directory) / "host.txt"
        host_file.write_text("host data", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_SANDBOX_PROBE_PATH"] = str(host_file)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        client._initialize_and_list_tools()
        client.send(r="probe sandbox")
        transcript = client._finish()

        assert host_file.read_text(encoding="utf-8") == "host data"
        return transcript


def test_shuts_down_stalled_worker(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        environment = os.environ.copy()
        temporary_path = Path(temporary_directory)
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_REPORT_PROCESS_GROUP"] = "1"
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        worker_group = None
        passed = False
        try:
            client._initialize_and_list_tools()
            stalled = client._start_send(
                r="stall",
                stdin="x" * (2 * 1024 * 1024),
            )
            stalled["send"]["stdin"] = "<large stdin>"
            group_marker = wait_for_marker(temporary_path, "zod-process-group", client)
            worker_group = read_worker_group(group_marker)
            wait_for_marker(temporary_path, "zod-stalled", client)
            client.stdin.close()
            try:
                return_code = client.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                raise AssertionError(
                    "mcp-console did not stop its stalled worker"
                ) from None

            assert return_code == 0, client.stderr.read()
            client.stdout.read()
            assert client.stderr.read() == ""
            assert not process_group_exists(worker_group), "Zod outlived mcp-console"
            passed = True
            return client.transcript
        finally:
            if not passed:
                stop_process_group(worker_group)
                stop_process(client.process)


def test_shutdown_deadline_does_not_wait_for_sideband_writer(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[1] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_BLOCK_SIDEBAND_WRITE"] = "1"
        environment["ZOD_REPORT_PROCESS_GROUP"] = "1"
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
        )
        worker_group = None
        passed = False
        try:
            client._initialize_and_list_tools()
            entry = client._start_send(r="x" * (2 * 1024 * 1024))
            group_marker = wait_for_marker(
                temporary_path,
                "zod-process-group",
                client,
            )
            worker_group = read_worker_group(group_marker)
            wait_for_marker(
                temporary_path,
                "zod-sideband-blocked",
                client,
            )
            entry["send"]["r"] = "<large cell>"
            shutdown_started = time.monotonic()
            client.stdin.close()
            try:
                return_code = client.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                raise AssertionError(
                    "mcp-console did not enforce its worker shutdown deadline"
                ) from None
            shutdown_elapsed = time.monotonic() - shutdown_started

            assert shutdown_elapsed < 1.5, (
                f"worker shutdown took {shutdown_elapsed:.3f} seconds"
            )
            assert return_code == 0, client.stderr.read()
            client.stdout.read()
            assert client.stderr.read() == ""
            assert not process_group_exists(worker_group), "Zod outlived mcp-console"
            passed = True
            return client.transcript
        finally:
            if not passed:
                stop_process_group(worker_group)
                stop_process(client.process)


def wait_for_marker(root: Path, name: str, client: McpClient) -> Path:
    deadline = time.monotonic() + 3
    while True:
        markers = list(root.glob(f"**/{name}"))
        if markers:
            assert len(markers) == 1, f"found multiple {name} markers"
            return markers[0]
        assert client.process.poll() is None, "mcp-console stopped before Zod stalled"
        assert time.monotonic() < deadline, "Zod did not report its stall checkpoint"
        time.sleep(0.01)


def read_worker_group(marker: Path) -> int:
    worker_group = int(marker.read_text(encoding="utf-8"))
    assert worker_group != os.getpgrp(), "Zod did not enter a dedicated process group"
    return worker_group


def process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for_process_group_exit(process_group: int, client: McpClient) -> None:
    deadline = time.monotonic() + 3
    while process_group_exists(process_group):
        assert client.process.poll() is None, "mcp-console stopped during restart"
        assert time.monotonic() < deadline, (
            "restart did not enforce its shutdown deadline"
        )
        time.sleep(0.01)


def stop_process_group(process_group: int | None) -> None:
    if process_group is None:
        return
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait()


if __name__ == "__main__":
    run_this_suite(__file__)
