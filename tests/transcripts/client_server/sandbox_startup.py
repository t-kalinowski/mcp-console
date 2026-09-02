#!/usr/bin/env -S uv run --script

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import (
    DarwinProcessIdentity,
    FifoCheckpoint,
    McpClient,
    Transcript,
    capture_darwin_process_identity,
    darwin_child_process_identities,
    darwin_process_waits_for_startup_release,
    kill_darwin_processes,
    live_darwin_processes,
    run_this_suite,
    stop_client,
)

PLATFORMS = {"darwin"}
TIMEOUT = 10
MARKER_NAME = "mcp-console-startup-marker"


def _build_manager_start_interposer(directory: Path) -> Path:
    source = directory / "manager-start-interposer.c"
    library = directory / "manager-start-interposer.dylib"
    source.write_text(
        r"""
#include <crt_externs.h>
#include <errno.h>
#include <fcntl.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

static _Atomic int gated_manager_read = 0;

static int is_subcommand(const char *name) {
    int argc = *_NSGetArgc();
    char **argv = *_NSGetArgv();
    return argc > 1 && strcmp(argv[1], name) == 0;
}

static void signal_checkpoint(const char *name) {
    const char *checkpoint = getenv(name);
    if (checkpoint == NULL) {
        _exit(125);
    }
    int descriptor = open(checkpoint, O_WRONLY | O_NONBLOCK);
    if (descriptor < 0) {
        _exit(125);
    }
    const char value = '1';
    ssize_t count;
    do {
        count = write(descriptor, &value, sizeof(value));
    } while (count < 0 && errno == EINTR);
    close(descriptor);
    if (count != sizeof(value)) {
        _exit(125);
    }
}

static void wait_for_release(const char *name) {
    const char *release = getenv(name);
    if (release == NULL) {
        _exit(125);
    }
    int descriptor;
    do {
        descriptor = open(release, O_RDONLY);
    } while (descriptor < 0 && errno == EINTR);
    if (descriptor < 0) {
        _exit(125);
    }
    char value;
    ssize_t count;
    do {
        count = read(descriptor, &value, sizeof(value));
    } while (count < 0 && errno == EINTR);
    close(descriptor);
    if (count != sizeof(value)) {
        _exit(125);
    }
}

static ssize_t gate_manager_initialization(
    int descriptor,
    void *buffer,
    size_t length,
    int flags
) {
    if (descriptor == STDIN_FILENO
        && is_subcommand("sandbox-manager")
        && atomic_exchange(&gated_manager_read, 1) == 0) {
        signal_checkpoint("MCP_CONSOLE_TEST_MANAGER_START");
        wait_for_release("MCP_CONSOLE_TEST_MANAGER_RELEASE");
    }
    return recvfrom(descriptor, buffer, length, flags, NULL, NULL);
}

#define DYLD_INTERPOSE(replacement, replacee)                                  \
    __attribute__((used)) static struct {                                      \
        const void *replacement;                                               \
        const void *replacee;                                                  \
    } interpose_##replacee __attribute__((section("__DATA,__interpose"))) = {  \
        (const void *)(uintptr_t)&replacement,                                 \
        (const void *)(uintptr_t)&replacee,                                    \
    };

DYLD_INTERPOSE(gate_manager_initialization, recv)
""".removeprefix("\n"),
        encoding="utf-8",
    )
    subprocess.run(
        [
            "cc",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Wpedantic",
            "-Werror",
            "-dynamiclib",
            "-o",
            library,
            source,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return library


def _worker_generation_processes(server_pid: int) -> tuple[int, int]:
    deadline = time.monotonic() + TIMEOUT
    while True:
        processes = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid=,command="],
            check=True,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        ).stdout
        manager = []
        root = []
        for process in processes.splitlines():
            fields = process.strip().split(maxsplit=2)
            if len(fields) != 3 or int(fields[1]) != server_pid:
                continue
            if "sandbox-manager" in fields[2]:
                manager.append(int(fields[0]))
            else:
                root.append(int(fields[0]))
        assert len(manager) <= 1, manager
        assert len(root) <= 1, root
        if manager and root:
            return root[0], manager[0]
        assert time.monotonic() < deadline, (
            "worker generation did not start its root and manager"
        )
        time.sleep(0.01)


def _wait_for_private_startup_gate(identity: DarwinProcessIdentity) -> None:
    deadline = time.monotonic() + TIMEOUT
    while not darwin_process_waits_for_startup_release(identity):
        assert live_darwin_processes((identity,)), (
            "relay root exited before reaching its private startup gate"
        )
        assert time.monotonic() < deadline, (
            "relay root did not block at its private startup gate"
        )
        time.sleep(0.01)


def _wait_for_startup_cleanup(
    identities: tuple[DarwinProcessIdentity, ...],
) -> None:
    deadline = time.monotonic() + TIMEOUT
    while True:
        survivors = live_darwin_processes(identities)
        if not survivors:
            return
        assert time.monotonic() < deadline, (
            f"startup cleanup left processes {survivors}"
        )
        time.sleep(0.01)


def _marker_record(temporary: Path) -> dict[str, object]:
    markers = list(temporary.glob(f"**/{MARKER_NAME}"))
    assert len(markers) == 1, markers
    record = json.loads(markers[0].read_text(encoding="utf-8"))
    assert record.keys() == {"pid", "extra_descriptors"}, record
    return record


def _assert_zod_echo(entry: dict[str, object]) -> None:
    result = entry["result"]
    assert result == {
        "content": [{"type": "text", "text": "zod: echo\n"}],
        "isError": False,
    }, result


def _run_startup_case(binary: Path, custom_relay: bool) -> Transcript:
    fixture_root = Path(__file__).resolve().parents[2] / "fixtures"
    worker = fixture_root / "zod"
    marker_relay = fixture_root / "startup_marker_relay"

    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        manager_started = FifoCheckpoint(temporary / "manager-started")
        manager_release = FifoCheckpoint(temporary / "manager-release")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_BINARY"] = str(binary)
        environment["MCP_CONSOLE_TEST_MANAGER_START"] = str(manager_started.path)
        environment["MCP_CONSOLE_TEST_MANAGER_RELEASE"] = str(manager_release.path)
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_manager_start_interposer(temporary)
        )

        arguments = ["serve", "--worker", str(worker)]
        if custom_relay:
            arguments.extend(["--relay", str(marker_relay)])
        client = McpClient(binary, tuple(arguments), environment)
        identities: tuple[DarwinProcessIdentity, ...] = ()
        released = False
        try:
            client._initialize_and_list_tools()
            waiting = client._start_send(r="echo echo")
            manager_started.wait("manager initialization")

            root_pid, manager_pid = _worker_generation_processes(client.process.pid)
            root = capture_darwin_process_identity(root_pid)
            manager = capture_darwin_process_identity(manager_pid)
            identities = (root, manager)
            _wait_for_private_startup_gate(root)
            assert darwin_child_process_identities(root) == (), (
                "worker started before sandbox supervision was ready"
            )
            markers = list(temporary.glob(f"**/{MARKER_NAME}"))
            assert markers == [], (
                "custom relay executed before sandbox supervision was ready"
            )

            manager_release.release()
            released = True
            client._receive(waiting)
            _assert_zod_echo(waiting)

            gate_record = {
                "manager": "blocked before readiness",
                "relay_root": "blocked on private startup gate",
                "worker": "not started before readiness",
            }
            if custom_relay:
                marker = _marker_record(temporary)
                assert marker == {
                    "pid": root_pid,
                    "extra_descriptors": [],
                }, marker
                gate_record["custom_relay"] = (
                    "executed after release in the gated root without the private gate"
                )
            waiting["startup_gate"] = gate_record
            return client._finish()
        finally:
            if not released:
                manager_release.release()
            stop_client(client)
            if identities:
                kill_darwin_processes(identities)
            manager_started.close()
            manager_release.close()


def test_builtin_relay_waits_for_supervision(binary: Path) -> Transcript:
    return _run_startup_case(binary, custom_relay=False)


def test_custom_relay_waits_for_supervision(binary: Path) -> Transcript:
    return _run_startup_case(binary, custom_relay=True)


def test_manager_failure_before_readiness_keeps_custom_relay_gated(
    binary: Path,
) -> Transcript:
    fixture_root = Path(__file__).resolve().parents[2] / "fixtures"
    worker = fixture_root / "zod"
    marker_relay = fixture_root / "startup_marker_relay"

    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        manager_started = FifoCheckpoint(temporary / "manager-started")
        manager_release = FifoCheckpoint(temporary / "manager-release")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_BINARY"] = str(binary)
        environment["MCP_CONSOLE_TEST_MANAGER_START"] = str(manager_started.path)
        environment["MCP_CONSOLE_TEST_MANAGER_RELEASE"] = str(manager_release.path)
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_manager_start_interposer(temporary)
        )

        client = McpClient(
            binary,
            (
                "serve",
                "--worker",
                str(worker),
                "--relay",
                str(marker_relay),
            ),
            environment,
        )
        identities: tuple[DarwinProcessIdentity, ...] = ()
        replacement_released = False
        try:
            client._initialize_and_list_tools()
            waiting = client._start_send(r="echo echo")
            manager_started.wait("manager initialization")

            root_pid, manager_pid = _worker_generation_processes(client.process.pid)
            root = capture_darwin_process_identity(root_pid)
            manager = capture_darwin_process_identity(manager_pid)
            identities = (root, manager)
            _wait_for_private_startup_gate(root)
            temporary_directories = tuple(
                temporary.glob(f"mcp-console-tmp-{client.process.pid}-*")
            )
            assert len(temporary_directories) == 1, temporary_directories
            assert list(temporary.glob(f"**/{MARKER_NAME}")) == []

            assert kill_darwin_processes((manager,)) == [manager_pid], (
                "sandbox manager exited before failure injection"
            )
            client._receive(waiting)
            result = waiting["result"]
            assert result.get("isError") is True, result
            assert (
                "sandbox manager did not become ready" in result["content"][0]["text"]
            ), result
            _wait_for_startup_cleanup(identities)
            assert list(temporary.glob(f"**/{MARKER_NAME}")) == []
            assert temporary_directories[0].exists(), (
                "ambiguous manager readiness removed the temporary directory"
            )
            waiting["startup_gate_failure"] = {
                "manager": "killed before readiness",
                "relay_root": "retired without executing the custom relay",
                "temporary_directory": "preserved",
            }

            replacement = client._start_send(r="echo echo")
            manager_started.wait("replacement manager initialization")
            manager_release.release()
            replacement_released = True
            client._receive(replacement)
            _assert_zod_echo(replacement)
            marker = _marker_record(temporary)
            assert marker["extra_descriptors"] == [], marker
            replacement["startup_gate_recovery"] = {
                "custom_relay": "executed only for the replacement generation",
                "private_gate": "closed before relay exec",
            }
            return client._finish()
        finally:
            if not replacement_released:
                manager_release.release()
            stop_client(client)
            if identities:
                kill_darwin_processes(identities)
            manager_started.close()
            manager_release.close()


if __name__ == "__main__":
    run_this_suite(__file__)
