"""Deterministic worker loop and scenario-local state."""

import errno
import io
import json
import os
import select
import signal
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path
from typing import TextIO

from .control import (
    close_test_cleanup_gate,
    close_test_control_channel,
    configure_test_fixture_control,
    duplicate_test_cleanup_gate,
    emit_test_event,
    publish_marker,
    response_gate_completed,
    wait_for_test_control,
)
from .io import (
    LARGE_OUTPUT_SIZE,
    discard_stdin_bytes,
    emit_large_output,
    fill_nonblocking,
    probe_pending_stdin,
    queued_stdin_bytes,
    write_all,
)
from .protocol import (
    close_sideband,
    open_sideband,
    send,
    send_output,
    wait_for_server_to_process_sideband,
)
from .startup import configure_startup
from .state import LoopAction, WorkerContext
from . import input as input_scenarios
from . import output as output_scenarios
from . import preparation as preparation_scenarios
from .output import PNG_1X1


def emit_background_stderr(root: Path, reader: TextIO, writer: TextIO) -> None:
    while not (root / "zod-release-background-stderr").exists():
        time.sleep(0.01)
    emit_large_output(2, b"zod background stderr\n")
    # Writing can finish with one pipe of bytes still unread. A further suffix
    # makes the intended payload reach the relay before the sideband round trip.
    write_all(2, b"y" * LARGE_OUTPUT_SIZE)
    wait_for_server_to_process_sideband(reader, writer)
    publish_marker(root / "zod-background-stderr-emitted")


def main() -> None:
    """Run the deterministic sideband fixture until the server shuts it down."""
    reader, writer = open_sideband()
    temporary = Path(tempfile.gettempdir())
    context = WorkerContext(temporary, reader, writer)
    configure_test_fixture_control(context)
    if started_marker := os.environ.get("MCP_CONSOLE_TEST_ZOD_STARTED"):
        publish_marker(Path(started_marker))

    def handle_sigint(_signum: int, _frame: object) -> None:
        context.received_sigint = True
        publish_marker(temporary / "zod-sigint-received")

    signal.signal(signal.SIGINT, handle_sigint)
    if os.environ.get("ZOD_REPORT_PID") == "1":
        publish_marker(temporary / "zod-worker-pid", str(os.getpid()))
    if os.environ.get("ZOD_REPORT_PROCESS_GROUP") == "1":
        publish_marker(
            temporary / "zod-process-group",
            str(os.getpgrp()),
        )
    startup_callback, startup_ready_sent = configure_startup(
        temporary,
        reader,
        writer,
    )
    if not startup_ready_sent:
        send(writer, {"kind": "ready"})
    if startup_callback:
        send(
            writer,
            {
                "kind": "resolve_python_version",
                "request": {"constraints": [">=3.11"]},
            },
        )
        response = json.loads(reader.readline())
        assert response["kind"] == "python_version_resolution_failed", response
        publish_marker(
            temporary / "zod-startup-callback-response",
            response["message"],
        )
    if os.environ.get("ZOD_BLOCK_NEXT_SIDEBAND_WRITE") == "1":
        command = wait_for_test_control(context, 0, "block_next_sideband_write")
        target_operation = command.get("target_operation")
        assert isinstance(target_operation, int), command
        context.block_next_sideband_write = target_operation
        context.retain_blocked_sideband = (
            os.environ.get("ZOD_RETAIN_BLOCKED_SIDEBAND") == "1"
        )

    while True:
        if context.block_next_sideband_write is not None:
            assert reader.read(1) != ""
            if context.retain_blocked_sideband:
                holder = os.fork()
                if holder == 0:
                    os.setsid()
                    for descriptor in (0, 1, 2):
                        os.close(descriptor)
                    publish_marker(
                        temporary / "zod-blocked-sideband-holder-pid",
                        str(os.getpid()),
                    )
                    while True:
                        signal.pause()
            emit_test_event(
                context,
                context.block_next_sideband_write,
                "sideband_reader_stalled",
                pid=os.getpid(),
                process_group=os.getpgrp(),
            )
            while True:
                signal.pause()
        if context.queued_message is None:
            line = reader.readline()
            if line == "":
                return
            message = json.loads(line)
        else:
            message = context.queued_message
            context.queued_message = None
        message_kind = message["kind"]
        if message_kind == "shutdown":
            return

        if message_kind == "prepare_r":
            preparation_scenarios.prepare_r(context, message)
            continue

        assert message_kind == "evaluate"
        assert set(message) == {"kind", "language", "source"}
        language = message["language"]
        source = message["source"]
        if record := os.environ.get("MCP_CONSOLE_TEST_ZOD_PYTHON_CELLS"):
            # Admission probes use real Python cells and record every evaluation.
            with Path(record).open("a", encoding="utf-8") as cells:
                cells.write(json.dumps(message) + "\n")
            assert language == "python", message
            with redirect_stdout(io.StringIO()) as output:
                exec(source, context.python_globals)
            if text := output.getvalue():
                send_output(writer, text)
            send(writer, {"kind": "completed"})
            continue
        if context.idle_input_received:
            assert (language, source) == ("r", "echo echo"), message
        if language in {"python", "sql"}:
            assert source.startswith("echo "), source
            payload = source.removeprefix("echo ")
            for output in (f"zod {language}: ", f"{payload}\n"):
                send_output(writer, output)
            send(writer, {"kind": "completed"})
            continue

        assert language == "r"
        if source == "fail sideband during shutdown":
            send(writer, {"kind": "completed"})
            # The client receives completion before releasing this write, so the
            # relay's gated read cannot also hold the completed frame.
            wait_for_test_control(context, 0, "emit_shutdown_failure")
            writer.write('{"kind":"console_output","data":}\n')
            writer.flush()
            while True:
                signal.pause()

        if source.startswith("check response gate: "):
            operation = int(source.removeprefix("check response gate: "))
            gate_released = response_gate_completed(context)
            emit_test_event(
                context,
                operation,
                "worker_operation_started",
                response_gate_released=gate_released,
            )
            send_output(writer, "zod response-gated operation\n")
            emit_test_event(context, operation, "worker_operation_completed")
            send(writer, {"kind": "completed"})
            continue

        if source.startswith("checkpoint "):
            operation = int(source.removeprefix("checkpoint "))
            emit_test_event(context, operation, "worker_operation_started")
            emit_test_event(context, operation, "worker_operation_completed")
            send(writer, {"kind": "completed"})
            continue

        if source.startswith("wait for interrupt: "):
            target = int(source.removeprefix("wait for interrupt: "))
            previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
            try:
                emit_test_event(context, target, "worker_operation_started")
                received = signal.sigwait({signal.SIGINT})
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            assert received == signal.SIGINT, received
            context.received_sigint = False
            emit_test_event(
                context, target, "worker_interrupt_observed", signal=received
            )
            send_output(writer, "zod interrupted\n")
            emit_test_event(context, target, "worker_operation_completed")
            send(writer, {"kind": "completed"})
            continue

        if source == "interrupt":
            publish_marker(temporary / "zod-interrupt-evaluation-started")
            while not context.received_sigint:
                time.sleep(0.01)
            context.received_sigint = False
            if release := os.environ.get("MCP_CONSOLE_TEST_INTERRUPT_RELEASE"):
                with Path(release).open("rb", buffering=0) as checkpoint:
                    assert checkpoint.read(1) == b"1"
            send_output(writer, "zod interrupted\n")
            send(writer, {"kind": "completed"})
            continue

        if source == "stall":
            publish_marker(temporary / "zod-stalled")
            while True:
                signal.pause()

        if source.startswith("stall: "):
            operation = int(source.removeprefix("stall: "))
            emit_test_event(context, operation, "worker_operation_started")
            emit_test_event(
                context,
                operation,
                "parent_operation_stalled",
                pid=os.getpid(),
                process_group=os.getpgrp(),
            )
            while True:
                signal.pause()

        if source == "close sideband with unread shutdown":
            send_output(writer, "zod waiting for shutdown\n")
            while os.read(0, 8192) != b"":
                pass
            expected = b'{"kind":"shutdown"}\n'
            # Leave the shutdown newline unread when both pipe directions close.
            queued = bytearray()
            while len(queued) < len(expected) - 1:
                chunk = os.read(reader.fileno(), len(expected) - 1 - len(queued))
                assert chunk, queued
                queued.extend(chunk)
            assert queued == expected[:-1], queued
            assert select.select([reader], [], [], 10)[0]
            close_sideband(reader, writer)
            return

        if source == "stall accepted relay shutdown":
            relay_pid = os.getppid()
            helper = os.fork()
            if helper == 0:
                os.setsid()
                close_sideband(reader, writer)
                for descriptor in (0, 1, 2):
                    os.close(descriptor)
                publish_marker(
                    temporary / "zod-relay-retirement-processes",
                    f"{os.getpid()} {relay_pid}",
                )
                while True:
                    signal.pause()
            assert os.read(0, 1) == b""
            # The relay's read checkpoint holds this output until reader join.
            # Keep the worker alive so shutdown must consume its grace period.
            write_all(1, b"zod output during relay retirement\n")
            while True:
                signal.pause()

        if source == "stall with stopped relay":
            relay_pid = int(os.environ["ZOD_RELAY_PID"])
            worker_pid = os.getpid()
            assert os.getppid() == relay_pid
            assert relay_pid != worker_pid
            assert os.getpgid(worker_pid) == os.getpgid(relay_pid) == os.getpgrp()
            helper = os.fork()
            if helper == 0:
                os.setsid()
                close_sideband(reader, writer)
                for descriptor in (0, 1, 2):
                    os.close(descriptor)
                publish_marker(
                    temporary / "zod-relay-stop-helper",
                    str(os.getpid()),
                )
                publish_marker(
                    temporary / "zod-relay-stop-target",
                    f"{relay_pid} {worker_pid}",
                )
                os.kill(relay_pid, signal.SIGSTOP)
                while True:
                    signal.pause()
            while True:
                signal.pause()

        if source == "violate protocol":
            send_output(writer, "zod output before protocol failure")
            send(writer, {"kind": "ready"})
            continue

        if source == "violate protocol after stdout":
            observed = os.open(
                os.environ["MCP_CONSOLE_TEST_RELAY_READ_BLOCKED"], os.O_RDWR
            )
            emit_large_output(1, b"zod old stdout\n")
            write_all(1, b"zod stdout tail\n")
            # The relay has read the complete payload, but the interposer holds
            # its final read until retirement joins the stdout reader.
            assert os.read(observed, 1) == b"1"
            os.close(observed)
            send(writer, {"kind": "ready"})
            continue

        if source == "unexpected input receipt after stdout":
            emit_large_output(1, b"zod unexpected input receipt: ")
            tail_size = fill_nonblocking(1, b"z" * LARGE_OUTPUT_SIZE)
            write_all(1, f"zod expected semantic tail: {tail_size:010d}\n".encode())
            send(writer, {"kind": "input_received"})
            while True:
                signal.pause()

        if source in {
            "malformed sideband after stdout",
            "malformed sideband after stderr",
        }:
            stream = source.removeprefix("malformed sideband after ")
            descriptor = 1 if stream == "stdout" else 2
            observed = os.open(
                os.environ["MCP_CONSOLE_TEST_RELAY_READ_BLOCKED"], os.O_RDWR
            )
            emit_large_output(descriptor, f"zod malformed {stream}: ".encode())
            tail_size = fill_nonblocking(descriptor, b"z" * LARGE_OUTPUT_SIZE)
            write_all(
                descriptor,
                f"zod expected {stream} malformed tail: {tail_size:010d}\n".encode(),
            )
            write_all(descriptor, b"zod malformed output read\n")
            # Hold the complete tail in the raw reader until retirement joins it.
            assert os.read(observed, 1) == b"1"
            os.close(observed)
            writer.write('{"kind":"console_output","data":}\n')
            writer.flush()
            while True:
                signal.pause()

        if source in {
            "force stop after raw stdout",
            "force stop after raw stderr",
        }:
            stream = source.removeprefix("force stop after raw ")
            descriptor = 1 if stream == "stdout" else 2
            write_all(
                descriptor,
                f"zod retiring {stream}: ".encode() + b"\xe2\x82",
            )
            send(writer, {"kind": "ready"})
            while True:
                signal.pause()

        if source == "exit unexpectedly":
            os._exit(86)

        if source == "overflow cell retention limit":
            output_scenarios.overflow_cell_retention_limit(context, source)
            continue

        if source == "overflow cell output file":
            output_scenarios.overflow_cell_output_file(context, source)
            continue

        if source == "overflow console output":
            output_scenarios.overflow_console_output(context, source)
            continue

        if source == "preview image limit":
            output_scenarios.preview_image_limit(context, source)
            continue

        if source in {"preview allocation image", "preview allocation image and text"}:
            output_scenarios.preview_allocation_image(context, source)
            continue

        if source == "preview rejected image":
            output_scenarios.preview_rejected_image(context, source)
            continue

        if source == "preview invalid oversized image":
            send_output(writer, "before invalid image\n")
            send(
                writer,
                {
                    "kind": "image",
                    "data": "A" * (8 * 1024 * 1024) + "AA?=",
                    "mime_type": "image/png",
                },
            )
            # If validation is skipped, completion returns a successful result
            # that the public test rejects. Fatal validation instead requests
            # shutdown; remain alive for the test's forced-retirement assertion.
            send(writer, {"kind": "completed"})
            response = json.loads(reader.readline())
            assert response == {"kind": "shutdown"}, response
            while True:
                signal.pause()

        if source == "preview recovery intervals":
            output_scenarios.preview_recovery_intervals(context, source)
            continue

        if source.startswith("preview recovery cell "):
            output_scenarios.preview_recovery_cell(context, source)
            continue

        if source == "preview alternating bytes":
            output_scenarios.preview_alternating_bytes(context, source)
            continue

        if source in {
            "preview same producer",
            "preview unicode suffix",
            "preview unicode replacement",
        }:
            output_scenarios.preview_producer_suffix(context, source)
            continue

        if source in {
            "preview huge line",
            "preview tiny events",
            "preview many tiny events",
            "preview redraw",
        }:
            output_scenarios.preview_large_output(context, source)
            continue

        if source == "exit zero":
            os._exit(0)

        if source == "kill relay and remain live":
            release = temporary / "zod-release-relay-exit"
            os.mkfifo(release)
            publish_marker(
                temporary / "zod-relay-exit-evaluation-started",
                f"{os.getpid()} {os.getppid()} {os.getpgrp()}",
            )
            with release.open("rb", buffering=0) as checkpoint:
                assert checkpoint.read(1) == b"1"
            worker_pid = os.getpid()
            relay_pid = int(os.environ["ZOD_RELAY_PID"])
            relay_group = os.getpgrp()
            assert worker_pid != relay_pid
            assert os.getppid() == relay_pid
            assert os.getpgid(relay_pid) == relay_group
            send_output(
                writer,
                (f"zod worker pid: {worker_pid}; relay process group: {relay_group}\n"),
            )
            wait_for_server_to_process_sideband(reader, writer)
            os.kill(relay_pid, signal.SIGKILL)
            while True:
                signal.pause()

        if source == "emit stdout":
            output_scenarios.emit_stdout(context, source)
            continue

        if source == "redraw across polls":
            output_scenarios.redraw_across_polls(context, source)
            continue

        if source == "stress redraws":
            output_scenarios.stress_redraws(context, source)
            continue

        if source in {"exit after invalid stdout", "exit after invalid stderr"}:
            stream = source.removeprefix("exit after invalid ")
            descriptor = 1 if stream == "stdout" else 2
            emit_large_output(
                descriptor,
                f"zod invalid {stream}: ".encode() + b"\xff trailing: \xe2\x82",
            )
            tail_size = fill_nonblocking(descriptor, b"z" * LARGE_OUTPUT_SIZE)
            write_all(
                descriptor,
                f"zod expected {stream} crash tail: {tail_size:010d}\n".encode(),
            )
            os._exit(86)

        if source == "start background stderr":
            temporary = Path(tempfile.gettempdir())
            child = os.fork()
            if child == 0:
                for descriptor in (0, 1):
                    os.close(descriptor)
                emit_background_stderr(temporary, reader, writer)
                os._exit(0)
            publish_marker(
                temporary / "zod-background-stderr-pid",
                str(child),
            )
            publish_marker(temporary / "zod-background-stderr-started")
            send(writer, {"kind": "completed"})
            child_pid, status = os.waitpid(child, 0)
            assert child_pid == child
            assert os.waitstatus_to_exitcode(status) == 0
            continue

        if source == "start background sideband" or source.startswith(
            "start background sideband: "
        ):
            marker = source.removeprefix("start background sideband")
            marker = marker.removeprefix(": ")
            suffix = f"-{marker}" if marker else ""
            child = os.fork()
            if child == 0:
                for descriptor in (0, 1, 2):
                    os.close(descriptor)
                release = temporary / f"zod-release-background-sideband{suffix}"
                while not release.exists():
                    time.sleep(0.01)
                if marker == "combined-requirements-failure":
                    send_output(writer, "idle before failure image\n")
                    send(
                        writer,
                        {
                            "kind": "image",
                            "data": PNG_1X1,
                            "mime_type": "image/png",
                        },
                    )
                    send_output(writer, "idle after failure image\n")
                else:
                    send_output(writer, "zod background sideband\n")
                wait_for_server_to_process_sideband(reader, writer)
                publish_marker(temporary / f"zod-background-sideband-emitted{suffix}")
                os._exit(0)
            send(writer, {"kind": "completed"})
            publish_marker(temporary / f"zod-background-sideband-started{suffix}")
            child_pid, status = os.waitpid(child, 0)
            assert child_pid == child
            assert os.waitstatus_to_exitcode(status) == 0
            continue

        if source in {
            "start partial sideband descendant",
            "exit after partial sideband descendant",
        }:
            release = temporary / "zod-release-partial-sideband"
            os.mkfifo(release)
            child = os.fork()
            if child == 0:
                os.setsid()
                reader.close()
                for descriptor in (0, 1, 2):
                    os.close(descriptor)
                publish_marker(
                    temporary / "zod-sideband-descendant-pid",
                    str(os.getpid()),
                )
                while True:
                    signal.pause()
            with release.open("rb", buffering=0) as stream:
                assert stream.read(1) == b"x"
            writer.write('{"kind":"console_output"')
            writer.flush()
            emit_test_event(context, 0, "partial_sideband_written")
            if source == "start partial sideband descendant":
                # Requested retirement closes stdin before the worker exits.
                assert os.read(0, 1) == b""
            os._exit(86)

        if source == "wait after readable frame and partial tail":
            cleanup_source = duplicate_test_cleanup_gate(context)
            child = os.fork()
            if child == 0:
                os.setsid()
                cleanup = os.dup(cleanup_source)
                os.close(cleanup_source)
                reader.close()
                for descriptor in (0, 1, 2):
                    os.close(descriptor)
                publish_marker(
                    temporary / "zod-sideband-descendant-pid",
                    str(os.getpid()),
                )
                assert os.read(cleanup, 1) in {b"", b"1"}
                os.close(cleanup)
                writer.close()
                os._exit(0)
            os.close(cleanup_source)
            close_test_cleanup_gate(context)
            send_output(writer, "zod readable retirement frame\n")
            writer.write('{"kind":"console_output"')
            writer.flush()
            publish_marker(temporary / "zod-sideband-partial-tail-written")
            assert os.read(0, 1) == b""
            os._exit(86)

        if source == "complete before partial sideband descendant":
            child = os.fork()
            if child == 0:
                os.setsid()
                reader.close()
                for descriptor in (0, 1, 2):
                    os.close(descriptor)
                publish_marker(
                    temporary / "zod-sideband-descendant-pid",
                    str(os.getpid()),
                )
                while True:
                    signal.pause()
            send(writer, {"kind": "completed"})
            writer.write('{"kind":"console_output"')
            writer.flush()
            publish_marker(temporary / "zod-sideband-partial-tail-written")
            continue

        if source.startswith("stall with detached stdin: "):
            operation = int(source.removeprefix("stall with detached stdin: "))
            emit_test_event(context, operation, "worker_operation_started")
            acknowledged, acknowledge = os.pipe()
            child = os.fork()
            if child == 0:
                os.setsid()
                os.close(acknowledged)
                retained_stdin = os.dup(0)
                assert os.fstat(retained_stdin) == os.fstat(0)
                os.close(0)
                cleanup = duplicate_test_cleanup_gate(context)
                close_test_cleanup_gate(context)
                close_test_control_channel(context)
                close_sideband(reader, writer)
                os.close(1)
                os.close(2)
                emit_test_event(
                    context,
                    operation,
                    "detached_descendant_created",
                    pid=os.getpid(),
                    process_group=os.getpgrp(),
                    inherited_fd=0,
                    retained_fd=retained_stdin,
                )
                assert os.write(acknowledge, b"1") == 1
                os.close(acknowledge)
                assert os.read(cleanup, 1) in {b"", b"1"}
                os.close(cleanup)
                os.close(retained_stdin)
                os._exit(0)
            os.close(acknowledge)
            assert os.read(acknowledged, 1) == b"1"
            os.close(acknowledged)
            close_test_cleanup_gate(context)
            consumed_bytes = 0
            emit_test_event(context, operation, "parent_waiting_for_stdin")
            while True:
                command = wait_for_test_control(context, operation, "probe_stdin")
                request = command["request"]
                expected_bytes = command["expected_bytes"]
                assert isinstance(request, int), command
                assert isinstance(expected_bytes, int), command
                assert expected_bytes > consumed_bytes, command
                consumed_bytes, queued_bytes = probe_pending_stdin(
                    expected_bytes,
                    consumed_bytes,
                )
                details = {
                    "target_operation": operation,
                    "expected_bytes": expected_bytes,
                    "consumed_bytes": consumed_bytes,
                    "queued_bytes": queued_bytes,
                }
                if consumed_bytes + queued_bytes < expected_bytes:
                    emit_test_event(context, request, "stdin_write_pending", **details)
                    break
                emit_test_event(context, request, "stdin_write_buffered", **details)
            emit_test_event(
                context,
                operation,
                "parent_operation_stalled",
                pid=os.getpid(),
                process_group=os.getpgrp(),
            )
            ownership = wait_for_test_control(
                context, operation, "observe_poll_ownership"
            )
            request = ownership["request"]
            prior_bytes = ownership["prior_bytes"]
            submitted_bytes = ownership["submitted_bytes"]
            sentinel = ownership["sentinel"]
            assert isinstance(request, int), ownership
            assert isinstance(prior_bytes, int), ownership
            assert isinstance(submitted_bytes, int), ownership
            assert isinstance(sentinel, str), ownership
            assert prior_bytes == expected_bytes, ownership
            assert submitted_bytes > 1, ownership
            sentinel_bytes = sentinel.encode()
            assert len(sentinel_bytes) == 1, ownership

            discard_stdin_bytes(prior_bytes - consumed_bytes)
            consumed_bytes = prior_bytes
            assert os.read(0, 1) == sentinel_bytes
            consumed_bytes += 1
            total_bytes = prior_bytes + submitted_bytes
            readable, _, _ = select.select([0], [], [], 15)
            assert readable, "poll ownership stdin tail did not reach Zod"
            queued_bytes = queued_stdin_bytes()
            assert 0 < queued_bytes < total_bytes - consumed_bytes, (
                queued_bytes,
                consumed_bytes,
                total_bytes,
            )
            emit_test_event(
                context,
                request,
                "poll_ownership_observed",
                target_operation=operation,
                consumed_bytes=consumed_bytes,
                queued_bytes=queued_bytes,
                submitted_bytes=submitted_bytes,
            )
            while True:
                signal.pause()

        if source == "request input":
            input_scenarios.request_input(context, source)
            continue

        if source == "preview prompt":
            input_scenarios.preview_prompt(context, source)
            continue

        if source == "language error":
            output_scenarios.language_error(context, source)
            continue

        if source == "wait for stdin close":
            publish_marker(temporary / "zod-waiting-for-stdin-close")
            assert os.read(0, 1) == b"", "Zod received input instead of stdin EOF"
            # Stdin closure and cell-log retirement travel through independent
            # transports. Emit only after both, so this is all unretained output.
            with Path(os.environ["ZOD_STDIN_CLOSE_RELEASE"]).open("rb") as release:
                assert release.read(1) == b"1"
            emit_large_output(1, b"zod stdin closed\n")
            send(writer, {"kind": "completed"})
            continue

        if source == "set controlled restart state":
            context.controlled_restart_state = "old"
            publish_marker(
                temporary / "zod-controlled-restart-old-worker",
                str(os.getpid()),
            )
            send_output(writer, "zod controlled state: old\n")
            send(writer, {"kind": "completed"})
            continue

        if source == "inspect controlled restart state":
            context.controlled_restart_evaluations += 1
            with (temporary / "zod-controlled-restart-cell-evaluations").open(
                "a",
                encoding="utf-8",
            ) as evaluations:
                evaluations.write(
                    f"{os.getpid()} {context.controlled_restart_state} "
                    f"{context.controlled_restart_evaluations}\n"
                )
            send_output(
                writer,
                f"zod controlled state: {context.controlled_restart_state}; "
                f"evaluation={context.controlled_restart_evaluations}\n",
            )
            send(writer, {"kind": "completed"})
            continue

        if source == "request input after timeout":
            input_scenarios.request_input_after_timeout(context, source)
            continue

        if source == "input without request then request input":
            input_scenarios.input_without_request_then_request_input(context, source)
            continue

        if source in {"input without request", "input length without request"}:
            input_scenarios.input_without_request(context, source)
            continue

        if source == "read fd 0 directly":
            input_scenarios.read_fd_zero_directly(context, source)
            continue

        if source == "probe sandbox":
            try:
                Path(os.environ["ZOD_SANDBOX_PROBE_PATH"]).write_text(
                    "escaped",
                    encoding="utf-8",
                )
            except OSError as error:
                assert error.errno == (
                    errno.EROFS if sys.platform == "linux" else errno.EPERM
                )
                output = "sandbox blocked host write\n"
            else:
                output = "worker escaped sandbox\n"
            send_output(writer, output)
            send(writer, {"kind": "completed"})
            continue

        if source == "complete silently":
            output_scenarios.complete_silently(context, source)
            continue

        if source == "shutdown output checkpoints":
            emit_test_event(context, 0, "evaluation_started")
            wait_for_test_control(context, 0, "emit_output")
            send_output(writer, "before shutdown\n")
            send(writer, {"kind": "image", "data": PNG_1X1, "mime_type": "image/png"})
            send_output(writer, "after image\n")
            wait_for_server_to_process_sideband(reader, writer)
            emit_test_event(context, 0, "output_processed")
            ownership = wait_for_test_control(context, 0, "observe_poll_ownership")
            request = ownership["request"]
            assert isinstance(request, int), ownership
            # Poll stdin reaches Zod only after the server claims the evaluation wait.
            assert os.read(0, 1) == b"p"
            emit_test_event(
                context, request, "poll_ownership_observed", target_operation=0
            )
            wait_for_test_control(context, 0, "complete")
            send(writer, {"kind": "completed"})
            wait_for_server_to_process_sideband(reader, writer)
            emit_test_event(context, 0, "completion_processed")
            continue

        if source == "complete before restart checkpoint":
            send(writer, {"kind": "completed"})
            wait_for_server_to_process_sideband(reader, writer)
            publish_marker(temporary / "zod-completion-processed")
            continue

        if source == "request input while idle":
            if (
                input_scenarios.request_input_while_idle(context, source)
                is LoopAction.STOP
            ):
                return
            continue

        if source == "resolve python while idle":
            preparation_scenarios.resolve_python_while_idle(context, source)
            continue

        if source == "report runtime R resolution failure":
            preparation_scenarios.report_runtime_r_resolution_failure(context, source)
            continue

        if source == "report process group":
            send_output(writer, f"zod process group: {os.getpgrp()}\n")
            send(writer, {"kind": "completed"})
            continue

        if source == "report managed R requirement":
            preparation_scenarios.report_managed_r_requirement(context, source)
            continue

        if source == "report raw R library bytes":
            preparation_scenarios.report_raw_r_library_bytes(context, source)
            continue

        if source == "fail next r preparation":
            preparation_scenarios.fail_next_r_preparation(context, source)
            continue

        if source == "fail next r preparation after output":
            preparation_scenarios.fail_next_r_preparation_after_output(context, source)
            continue

        if source == "report managed python activation":
            preparation_scenarios.report_managed_python_activation(context, source)
            continue

        if source == "emit console kinds":
            output_scenarios.emit_console_kinds(context, source)
            continue

        if source == "emit image":
            output_scenarios.emit_image(context, source)
            continue

        if source in {
            "emit image before completion",
            "emit output and image before completion",
        }:
            output_scenarios.emit_image_before_completion(context, source)
            continue

        if source == "complete after timeout":
            time.sleep(0.25)
        elif source == "complete after release":
            publish_marker(temporary / "zod-evaluation-started")
            while not (temporary / "zod-release-evaluation").exists():
                time.sleep(0.01)
        elif source == "output then complete after release":
            release = temporary / "zod-release-cell-output"
            os.mkfifo(release)
            publish_marker(temporary / "zod-cell-output-pending")
            with release.open("rb", buffering=0) as checkpoint:
                assert checkpoint.read(1) == b"1"
            send_output(writer, "zod cell output before completion\n")
            wait_for_server_to_process_sideband(reader, writer)
            publish_marker(temporary / "zod-cell-output-processed")
            while not (temporary / "zod-release-evaluation").exists():
                time.sleep(0.01)
        elif not source.startswith("echo "):
            raise AssertionError(f"unsupported Zod command: {source}")

        payload = source.removeprefix("echo ")
        for output in ("zod: ", f"{payload}\n"):
            send_output(writer, output)
        send(writer, {"kind": "completed"})
        if context.idle_input_received:
            wait_for_server_to_process_sideband(reader, writer)
            publish_marker(temporary / "zod-idle-input-received")
            context.idle_input_received = False
