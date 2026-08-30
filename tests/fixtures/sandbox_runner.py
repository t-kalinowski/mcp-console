#!/usr/bin/env python3

import errno
import json
import os
import select
import shutil
import struct
import sys
import time
from pathlib import Path


MAX_FRAME_SIZE = 1_048_576
PROTOCOL_VERSION = 1

if sys.platform == "darwin":
    OPERATING_SYSTEM = "macos"
    BACKEND = "macos_seatbelt"
    WRONG_BACKEND = "linux_bubblewrap"
    SIGNAL_LIFECYCLE = True
    REQUIRED_COMPANIONS: list[dict[str, object]] = []
    ROOT_PROCESS_ID: int | None = 1234
elif sys.platform.startswith("linux"):
    OPERATING_SYSTEM = "linux"
    BACKEND = "linux_bubblewrap"
    WRONG_BACKEND = "macos_seatbelt"
    SIGNAL_LIFECYCLE = False
    REQUIRED_COMPANIONS = [
        {
            "name": "bubblewrap",
            "relative_path": "codex-resources/bwrap",
            "required": True,
        }
    ]
    ROOT_PROCESS_ID = None
else:
    raise RuntimeError(f"unsupported fixture platform: {sys.platform}")


def read_exact(control: object, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = control.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(control: object) -> dict[str, object] | None:
    header = read_exact(control, 4)
    if not header:
        return None
    if len(header) != 4:
        raise RuntimeError("truncated request header")
    length = struct.unpack(">I", header)[0]
    payload = read_exact(control, length)
    if len(payload) != length:
        raise RuntimeError("truncated request payload")
    return json.loads(payload)


def write_frame(control: object, payload: dict[str, object]) -> None:
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    control.write(struct.pack(">I", len(encoded)))
    control.write(encoded)
    control.flush()


def capabilities(source_revision: str, behavior: str) -> dict[str, object]:
    protocol_version = 2 if behavior == "wrong_protocol" else PROTOCOL_VERSION
    revision = "0" * 40 if behavior == "wrong_revision" else source_revision
    backend = "unsupported" if behavior == "unsupported_backend" else BACKEND
    setup_state = "unsupported" if behavior == "unsupported_backend" else "not_required"
    companions = [companion.copy() for companion in REQUIRED_COMPANIONS]
    if behavior == "unexpected_companion":
        companions.append(
            {
                "name": "unexpected",
                "relative_path": "codex-resources/unexpected",
                "required": True,
            }
        )
    supported = behavior != "unsupported_backend"
    signal_lifecycle = supported and SIGNAL_LIFECYCLE
    if behavior == "wrong_lifecycle":
        signal_lifecycle = not signal_lifecycle
    return {
        "protocol_version": protocol_version,
        "maximum_frame_size": 512 if behavior == "wrong_frame_size" else MAX_FRAME_SIZE,
        "runner_version": "0.150.1",
        "codex_source_revision": revision,
        "codex_release_tag": "rust-v0.150.1",
        "operating_system": OPERATING_SYSTEM,
        "architecture": "fixture",
        "backend": backend,
        "filesystem": {
            "platform_minimal": supported,
            "host_read_only": supported,
            "read_rules": supported,
            "write_rules": supported,
            "deny_read_rules": supported,
            "deny_write_rules": supported,
            "missing_path_error": supported,
            "missing_path_ignore": supported,
            "precedence": "more_specific_then_deny_then_write_then_read",
            "state_directory_protected": supported,
            "unicode_policy_paths_only": True,
        },
        "network": {
            "denied": supported,
            "direct_egress_confinement": supported,
        },
        "streams": {
            "inherited": supported,
            "passed_handle": supported,
            "null": supported,
            "independent": supported,
            "byte_transparent": supported,
            "application_bytes_on_control_channel": False,
        },
        "terminal": {
            "inherited_terminal": supported,
            "caller_supplied_pty": supported,
            "controlling_terminal_reopen": False,
            "pty_creation_inside_sandbox": supported,
            "host_device_isolation": False,
        },
        "lifecycle": {
            "interrupt": signal_lifecycle,
            "graceful_termination": supported and SIGNAL_LIFECYCLE,
            "forced_termination": supported,
            "root_exit_observation": supported,
            "process_tree_supervision": supported,
            "full_tree_retirement": supported,
            "cleanup_after_root_exit": supported,
            "control_loss_retires_target": supported,
        },
        "required_companions": companions,
        "setup": {"state": setup_state, "detail": None},
    }


def main() -> int:
    behavior = os.environ.get("MCP_CONSOLE_FAKE_RUNNER_BEHAVIOR", "success")
    log_path = Path(os.environ["MCP_CONSOLE_FAKE_RUNNER_LOG"])
    expected_revision = os.environ["MCP_CONSOLE_EXPECTED_CODEX_REVISION"]
    arguments = sys.argv[1:]
    control_index = arguments.index("--control-fd")
    control_fd = int(arguments[control_index + 1])
    state_directory = Path(arguments[arguments.index("--state-dir") + 1])
    cleanup_directory = Path(arguments[arguments.index("--cleanup-dir") + 1])
    separator = arguments.index("--")
    target = arguments[separator + 1 :]
    target_bytes = [os.fsencode(argument).hex() for argument in target]
    target = [
        os.fsencode(argument).decode("utf-8", errors="backslashreplace")
        for argument in target
    ]
    stream_fds = [
        int(arguments[index + 1])
        for index, argument in enumerate(arguments)
        if argument == "--stream-fd"
    ]
    record: dict[str, object] = {
        "bootstrap": {
            "runner_pid": os.getpid(),
            "runner_process_group": os.getpgrp(),
            "state_directory": str(state_directory),
            "cleanup_directory": str(cleanup_directory),
            "control_fd": control_fd,
            "stream_fds": stream_fds,
            "target": target,
            "target_bytes": target_bytes,
        },
        "requests": [],
        "events": [],
    }

    def save() -> None:
        log_path.write_text(json.dumps(record), encoding="utf-8")

    def remove_cleanup(event: str) -> None:
        shutil.rmtree(cleanup_directory)
        events = record["events"]
        assert isinstance(events, list)
        events.append(event)
        save()

    save()
    if behavior == "late_inherited_descriptor":
        descriptor = int(os.environ["MCP_CONSOLE_TEST_AT_FORK_FD"])
        try:
            os.write(descriptor, b"escaped")
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
        else:
            events = record["events"]
            assert isinstance(events, list)
            events.append("late_descriptor_inherited")
            save()
            return 90
    if behavior == "exit_before_discovery":
        return 41

    control = os.fdopen(control_fd, "r+b", buffering=0)
    requests = record["requests"]
    assert isinstance(requests, list)

    while request := read_frame(control):
        requests.append(request)
        save()
        request_id = request["id"]
        operation = request["type"]

        if operation == "discover":
            if behavior == "malformed_discovery":
                control.write(struct.pack(">I", 1) + b"{")
                return 42
            if behavior == "truncated_discovery":
                control.write(struct.pack(">I", 100) + b"{}")
                return 43
            write_frame(
                control,
                {
                    "type": "capabilities",
                    "id": request_id,
                    "capabilities": capabilities(expected_revision, behavior),
                },
            )
        elif operation == "launch":
            root_process_id = (
                None if behavior == "missing_root_process_id" else ROOT_PROCESS_ID
            )
            write_frame(
                control,
                {
                    "type": "launch_accepted",
                    "id": request_id,
                    "backend": WRONG_BACKEND
                    if behavior == "wrong_launch_backend"
                    else BACKEND,
                    "root_process_id": root_process_id,
                },
            )
            if behavior == "exit_after_launch":
                return 44
            if behavior == "control_loss":
                remove_cleanup("cleanup_before_control_loss_exit")
                control.close()
                return 0
            if behavior == "direct_streams":
                streams = request["launch"]["streams"]
                stdin_fd = streams["stdin"]["handle"]
                stdout_fd = streams["stdout"]["handle"]
                stderr_fd = streams["stderr"]["handle"]
                chunks: list[bytes] = []
                while chunk := os.read(stdin_fd, 4096):
                    chunks.append(chunk)
                os.close(stdin_fd)
                payload = b"".join(chunks)
                os.write(stdout_fd, b"stdout:\x00" + payload + b"\xff")
                os.write(stderr_fd, b"stderr:\x00" + payload + b"\xfe")
                os.close(stdout_fd)
                os.close(stderr_fd)
            if behavior == "wrong_interrupt_ack":
                stdout_fd = request["launch"]["streams"]["stdout"]["handle"]
                os.write(stdout_fd, b"ready\n")
        elif operation == "status":
            if behavior == "malformed_status":
                control.write(struct.pack(">I", 1) + b"{")
                return 45
            if behavior == "truncated_status":
                control.write(struct.pack(">I", 100) + b"{}")
                return 46
            if behavior == "delayed_status":
                time.sleep(0.25)
            if behavior == "stalled_status":
                if select.select([control], [], [], 3)[0]:
                    assert control.read(1) == b""
                    remove_cleanup("cleanup_after_status_timeout")
                    return 0
                return 47
            phase = {
                "invalid_status_phase": "future",
                "wrong_interrupt_ack": "running",
            }.get(behavior, "root_exited")
            write_frame(
                control,
                {
                    "type": "status",
                    "id": request_id,
                    "status": {
                        "phase": phase,
                        "target": {
                            "kind": "exited",
                            "code": 0,
                            "signal": None,
                            "error": None,
                        },
                        "retirement": None,
                    },
                },
            )
        elif operation == "interrupt":
            write_frame(
                control,
                {
                    "type": "acknowledged",
                    "id": request_id,
                    "operation": (
                        "terminate"
                        if behavior == "wrong_interrupt_ack"
                        else "interrupt"
                    ),
                },
            )
            if behavior == "wrong_interrupt_ack":
                remove_cleanup("cleanup_after_wrong_interrupt_ack")
                control.close()
                return 0
        elif operation == "terminate":
            write_frame(
                control,
                {
                    "type": "acknowledged",
                    "id": request_id,
                    "operation": "terminate",
                },
            )
        elif operation == "wait":
            if behavior == "cleanup_failure":
                cleanup_error = "fixture cleanup failed"
            else:
                remove_cleanup("cleanup_before_final")
                cleanup_error = None
            target_kind = (
                "signaled"
                if behavior in {"target_signal", "missing_signal", "signaled_with_code"}
                else "exited"
            )
            target_code = 17 if behavior == "target_exit" else 0
            target_signal = 15 if behavior == "target_signal" else None
            if target_kind == "signaled":
                target_code = 0 if behavior == "signaled_with_code" else None
            if behavior == "missing_exit_code":
                target_code = None
            if behavior == "exited_with_signal":
                target_signal = 15
            write_frame(
                control,
                {
                    "type": "final",
                    "id": request_id,
                    "outcome": {
                        "target": {
                            "kind": target_kind,
                            "code": target_code,
                            "signal": target_signal,
                            "error": None,
                        },
                        "retirement": {
                            "complete": True,
                            "forced": False,
                            "error": None,
                        },
                        "infrastructure": {
                            "error": None,
                            "cleanup_error": cleanup_error,
                        },
                    },
                },
            )
            return 0
        else:
            raise RuntimeError(f"unexpected operation: {operation}")
    if cleanup_directory.exists():
        remove_cleanup("cleanup_after_control_eof")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
