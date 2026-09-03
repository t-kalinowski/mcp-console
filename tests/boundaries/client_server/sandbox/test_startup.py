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

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _support import (
    DarwinProcessIdentity,
    FifoCheckpoint,
    McpClient,
    Transcript,
    build_manager_interposer,
    capture_darwin_process_identity,
    darwin_child_process_identities,
    darwin_process_waits_for_startup_release,
    kill_darwin_processes,
    live_darwin_processes,
    run_this_suite,
    signal_darwin_process,
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
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static _Atomic int gated_manager_start = 0;
static _Atomic int server_fork_count = 0;
static _Atomic pid_t denied_cleanup_root = 0;
static _Atomic int reported_direct_cleanup_denial = 0;

typedef int (*kill_function)(pid_t, int);
typedef int (*killpg_function)(pid_t, int);

static kill_function next_kill(void) {
    return kill;
}

static killpg_function next_killpg(void) {
    return killpg;
}

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

static pid_t gate_manager_start(void) {
    if (is_subcommand("sandbox-manager")
        && atomic_exchange(&gated_manager_start, 1) == 0) {
        signal_checkpoint("MCP_CONSOLE_TEST_MANAGER_START");
        wait_for_release("MCP_CONSOLE_TEST_MANAGER_RELEASE");
    }
    return getppid();
}

static pid_t gate_manager_spawn(void) {
    int fork_index = atomic_fetch_add(&server_fork_count, 1);
    if (is_subcommand("serve")
        && getenv("MCP_CONSOLE_TEST_MANAGER_SPAWN") != NULL
        && fork_index == 1) {
        signal_checkpoint("MCP_CONSOLE_TEST_MANAGER_SPAWN");
        wait_for_release("MCP_CONSOLE_TEST_MANAGER_SPAWN_RELEASE");
    }
    return fork();
}

static int deny_startup_cleanup_group(pid_t process_group_id, int number) {
    if (number == SIGKILL
        && is_subcommand("serve")
        && getenv("MCP_CONSOLE_TEST_DENY_STARTUP_CLEANUP") != NULL) {
        atomic_store(&denied_cleanup_root, process_group_id);
        errno = EIO;
        return -1;
    }
    killpg_function killpg_next = next_killpg();
    return killpg_next(process_group_id, number);
}

static int deny_startup_cleanup_process(pid_t process_id, int number) {
    if (number == SIGKILL
        && process_id == atomic_load(&denied_cleanup_root)
        && is_subcommand("serve")
        && getenv("MCP_CONSOLE_TEST_DENY_STARTUP_CLEANUP") != NULL) {
        if (atomic_exchange(&reported_direct_cleanup_denial, 1) == 0) {
            signal_checkpoint("MCP_CONSOLE_TEST_DIRECT_KILL_DENIED");
        }
        errno = EPERM;
        return -1;
    }
    kill_function kill_next = next_kill();
    return kill_next(process_id, number);
}

#define DYLD_INTERPOSE(replacement, replacee)                                  \
    __attribute__((used)) static struct {                                      \
        const void *replacement;                                               \
        const void *replacee;                                                  \
    } interpose_##replacee __attribute__((section("__DATA,__interpose"))) = {  \
        (const void *)(uintptr_t)&replacement,                                 \
        (const void *)(uintptr_t)&replacee,                                    \
    };

DYLD_INTERPOSE(gate_manager_start, getppid)
DYLD_INTERPOSE(gate_manager_spawn, fork)
DYLD_INTERPOSE(deny_startup_cleanup_group, killpg)
DYLD_INTERPOSE(deny_startup_cleanup_process, kill)
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


def _wait_for_process_state(
    identity: DarwinProcessIdentity,
    prefix: str,
    description: str,
) -> None:
    deadline = time.monotonic() + TIMEOUT
    while True:
        assert live_darwin_processes((identity,)) == [identity[0]], (
            f"{description} exited before reaching state {prefix!r}"
        )
        result = subprocess.run(
            ["/bin/ps", "-o", "state=", "-p", str(identity[0])],
            capture_output=True,
            text=True,
            check=True,
            timeout=TIMEOUT,
        )
        if result.stdout.strip().startswith(prefix):
            return
        assert time.monotonic() < deadline, (
            f"timed out waiting for {description} state {prefix!r}"
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
    fixture_root = Path(__file__).resolve().parents[3] / "fixtures"
    worker = fixture_root / "zod"
    marker_relay = fixture_root / "startup_marker_relay"

    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        manager_spawn = FifoCheckpoint(temporary / "manager-spawn")
        manager_spawn_release = FifoCheckpoint(temporary / "manager-spawn-release")
        manager_started = FifoCheckpoint(temporary / "manager-started")
        manager_release = FifoCheckpoint(temporary / "manager-release")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_BINARY"] = str(binary)
        environment["MCP_CONSOLE_TEST_MANAGER_START"] = str(manager_started.path)
        environment["MCP_CONSOLE_TEST_MANAGER_RELEASE"] = str(manager_release.path)
        environment["MCP_CONSOLE_TEST_MANAGER_SPAWN"] = str(manager_spawn.path)
        environment["MCP_CONSOLE_TEST_MANAGER_SPAWN_RELEASE"] = str(
            manager_spawn_release.path
        )
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_manager_start_interposer(temporary)
        )

        arguments = ["serve", "--worker", str(worker)]
        if custom_relay:
            arguments.extend(["--relay", str(marker_relay)])
        client = McpClient(binary, tuple(arguments), environment)
        identities: tuple[DarwinProcessIdentity, ...] = ()
        manager_spawn_released = False
        manager_start_released = False
        try:
            client._initialize_and_list_tools()
            waiting = client._start_send(r="echo echo")
            manager_spawn.wait("manager spawn")

            server = capture_darwin_process_identity(client.process.pid)
            children = darwin_child_process_identities(server)
            assert len(children) == 1, children
            root = children[0]
            identities = (root,)
            _wait_for_private_startup_gate(root)
            assert darwin_child_process_identities(root) == (), (
                "worker started before sandbox supervision was ready"
            )
            markers = list(temporary.glob(f"**/{MARKER_NAME}"))
            assert markers == [], (
                "custom relay executed before sandbox supervision was ready"
            )

            manager_spawn_release.release()
            manager_spawn_released = True
            manager_started.wait("manager startup")

            root_pid, manager_pid = _worker_generation_processes(client.process.pid)
            assert root_pid == root[0], (root_pid, root)
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
            manager_start_released = True
            client._receive(waiting)
            _assert_zod_echo(waiting)

            gate_record = {
                "manager": "started after relay root and blocked before readiness",
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
            if not manager_spawn_released:
                manager_spawn_release.release()
            if not manager_start_released:
                manager_release.release()
            stop_client(client)
            if identities:
                kill_darwin_processes(identities)
            manager_spawn.close()
            manager_spawn_release.close()
            manager_started.close()
            manager_release.close()


def test_builtin_relay_waits_for_supervision(binary: Path) -> Transcript:
    return _run_startup_case(binary, custom_relay=False)


def test_custom_relay_waits_for_supervision(binary: Path) -> Transcript:
    return _run_startup_case(binary, custom_relay=True)


def test_custom_relay_starts_after_manager_readiness(binary: Path) -> Transcript:
    fixture_root = Path(__file__).resolve().parents[3] / "fixtures"
    worker = fixture_root / "zod"
    marker_relay = fixture_root / "startup_marker_relay"

    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        ready_sent = FifoCheckpoint(temporary / "ready-sent")
        ready_return = FifoCheckpoint(temporary / "ready-return")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_BINARY"] = str(binary)
        environment["MCP_CONSOLE_TEST_MANAGER_READY_SENT"] = str(ready_sent.path)
        environment["MCP_CONSOLE_TEST_MANAGER_READY_RETURN"] = str(ready_return.path)
        environment["DYLD_INSERT_LIBRARIES"] = str(build_manager_interposer(temporary))

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
        manager_released = False
        try:
            client._initialize_and_list_tools()
            waiting = client._start_send(r="echo echo")
            ready_sent.wait("manager READY delivery")

            readable, _, _ = select.select([client.stdout], [], [], TIMEOUT)
            assert readable, "custom relay waited for another manager response"
            client._receive(waiting)
            _assert_zod_echo(waiting)
            marker = _marker_record(temporary)
            assert marker["extra_descriptors"] == [], marker
            waiting["startup_gate"] = {
                "manager": "READY delivered with send return held",
                "custom_relay": "executed without another manager response",
                "private_gate": "closed before relay exec",
            }

            ready_return.release()
            manager_released = True
            return client._finish()
        finally:
            if not manager_released:
                ready_return.release()
            stop_client(client)
            ready_sent.close()
            ready_return.close()


def test_manager_failure_before_readiness_keeps_custom_relay_gated(
    binary: Path,
) -> Transcript:
    fixture_root = Path(__file__).resolve().parents[3] / "fixtures"
    worker = fixture_root / "zod"
    marker_relay = fixture_root / "startup_marker_relay"

    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        manager_started = FifoCheckpoint(temporary / "manager-started")
        manager_release = FifoCheckpoint(temporary / "manager-release")
        direct_kill_denied = FifoCheckpoint(temporary / "direct-kill-denied")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_BINARY"] = str(binary)
        environment["MCP_CONSOLE_TEST_MANAGER_START"] = str(manager_started.path)
        environment["MCP_CONSOLE_TEST_MANAGER_RELEASE"] = str(manager_release.path)
        environment["MCP_CONSOLE_TEST_DENY_STARTUP_CLEANUP"] = "1"
        environment["MCP_CONSOLE_TEST_DIRECT_KILL_DENIED"] = str(
            direct_kill_denied.path
        )
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
            manager_started.wait("manager startup")

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

            assert signal_darwin_process(root, signal.SIGSTOP), (
                "sandbox root exited before stop injection"
            )
            _wait_for_process_state(root, "T", "sandbox root")
            assert kill_darwin_processes((manager,)) == [manager_pid], (
                "sandbox manager exited before failure injection"
            )
            direct_kill_denied.wait("direct sandbox-root signal denial")
            assert signal_darwin_process(root, signal.SIGCONT), (
                "sandbox root exited before gate-close verification"
            )
            readable, _, _ = select.select([client.stdout], [], [], TIMEOUT)
            assert readable, (
                "server did not return after startup cleanup signals failed"
            )
            client._receive(waiting)
            result = waiting["result"]
            assert result.get("isError") is True, result
            text = result["content"][0]["text"]
            assert "sandbox manager did not become ready" in text, result
            assert (
                "failed to stop `/usr/bin/sandbox-exec` process group: "
                "Input/output error" in text
            ), result
            assert (
                "failed to stop `/usr/bin/sandbox-exec`: Operation not permitted"
                in text
            ), result
            _wait_for_startup_cleanup(identities)
            assert list(temporary.glob(f"**/{MARKER_NAME}")) == []
            assert temporary_directories[0].exists(), (
                "ambiguous manager readiness removed the temporary directory"
            )
            waiting["startup_gate_failure"] = {
                "manager": "killed before readiness",
                "cleanup_signals": "EIO for group, EPERM for direct root",
                "root_signal": "SIGSTOP then SIGCONT",
                "relay_root": "retired without executing the custom relay",
                "temporary_directory": "preserved",
            }

            replacement = client._start_send(r="echo echo")
            manager_started.wait("replacement manager startup")
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
            direct_kill_denied.close()


if __name__ == "__main__":
    run_this_suite(__file__)
