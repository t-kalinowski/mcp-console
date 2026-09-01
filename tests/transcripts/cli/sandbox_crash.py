#!/usr/bin/env -S uv run --script

import os
import re
import select
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import (
    DarwinProcessIdentity,
    FifoCheckpoint,
    Transcript,
    capture_darwin_process_identity,
    code,
    darwin_child_process_identities,
    darwin_process_waits_for_startup_release,
    kill_darwin_processes,
    live_darwin_processes,
    run_this_suite,
    signal_darwin_process,
)

PLATFORMS = {"darwin"}
TIMEOUT = 10


@dataclass
class _SandboxLifetime:
    process: subprocess.Popen[bytes]
    arguments: tuple[str, ...]
    launcher: DarwinProcessIdentity
    root: DarwinProcessIdentity
    descendant: DarwinProcessIdentity
    manager: DarwinProcessIdentity
    temporary_directory: Path


def _command(*arguments: str) -> list[str]:
    return ["mcp-console", *arguments]


def _build_supervision_interposer(directory: Path, behavior: str) -> Path:
    definitions = {
        "manager-start": "-DMCP_CONSOLE_INTERPOSE_MANAGER_START",
        "denied-sigkill": "-DMCP_CONSOLE_INTERPOSE_DENIED_SIGKILL",
        "failed-recovery-stop": "-DMCP_CONSOLE_INTERPOSE_FAILED_RECOVERY_STOP",
        "failed-root-observer": "-DMCP_CONSOLE_INTERPOSE_FAILED_ROOT_OBSERVER",
        "late-cleanup": "-DMCP_CONSOLE_INTERPOSE_LATE_CLEANUP",
        "retirement-disposition": ("-DMCP_CONSOLE_INTERPOSE_RETIREMENT_DISPOSITION"),
    }
    assert behavior in definitions, behavior
    source = directory / "supervision-interposer.c"
    library = directory / "supervision-interposer.dylib"
    source.write_text(
        r"""
#include <crt_externs.h>
#include <errno.h>
#include <fcntl.h>
#include <libproc.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#if defined(MCP_CONSOLE_INTERPOSE_DENIED_SIGKILL) \
    || defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP)
static _Atomic int denied_sigkill = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP)
static _Atomic int delayed_cleanup = 0;
static _Atomic int delayed_late_recovery = 0;
static _Atomic int reaped_root = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_START)
static _Atomic int gated_manager_read = 0;
static _Atomic int failed_target_release = 0;
static int retained_target_gate = -1;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_DISPOSITION)
static _Atomic int gated_retirement_disposition = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_FAILED_RECOVERY_STOP)
static _Atomic int failed_process_info = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_FAILED_ROOT_OBSERVER)
static _Atomic int process_info_calls = 0;
#endif

#if defined(MCP_CONSOLE_INTERPOSE_DENIED_SIGKILL) \
    || defined(MCP_CONSOLE_INTERPOSE_FAILED_ROOT_OBSERVER) \
    || defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP)
typedef int (*kill_function)(pid_t, int);

static kill_function next_kill(void) {
    return kill;
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_FAILED_RECOVERY_STOP) \
    || defined(MCP_CONSOLE_INTERPOSE_FAILED_ROOT_OBSERVER) \
    || defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP)
typedef int (*proc_pidinfo_function)(int, int, uint64_t, void *, int);

static proc_pidinfo_function next_proc_pidinfo(void) {
    return proc_pidinfo;
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_FAILED_ROOT_OBSERVER)
static void signal_checkpoint(const char *name);

static int fail_root_observer(
    int process_id,
    int flavor,
    uint64_t argument,
    void *buffer,
    int buffer_size
) {
    if (flavor == PROC_PIDTBSDINFO
        && atomic_fetch_add(&process_info_calls, 1) == 1) {
        signal_checkpoint("MCP_CONSOLE_TEST_PROCESS_INFO_FAILURE");
        errno = EIO;
        return 0;
    }
    return next_proc_pidinfo()(process_id, flavor, argument, buffer, buffer_size);
}

static int fail_root_group_stop(pid_t process_id, int number) {
    if (process_id < 0 && number == SIGKILL) {
        signal_checkpoint("MCP_CONSOLE_TEST_GROUP_STOP_FAILURE");
        errno = EIO;
        return -1;
    }
    return next_kill()(process_id, number);
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_FAILED_RECOVERY_STOP)
static void signal_checkpoint(const char *name);

static int fail_group_stop(pid_t process_group_id, int number) {
    const char *trigger = getenv("MCP_CONSOLE_TEST_PROCESS_INFO_FAILURE_TRIGGER");
    if (number == SIGKILL && trigger != NULL && access(trigger, F_OK) == 0) {
        signal_checkpoint("MCP_CONSOLE_TEST_GROUP_STOP_FAILURE");
        errno = EIO;
        return -1;
    }
    return killpg(process_group_id, number);
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP)
typedef pid_t (*waitpid_function)(pid_t, int *, int);

static waitpid_function next_waitpid(void) {
    return waitpid;
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_START)
typedef ssize_t (*send_function)(int, const void *, size_t, int);

static send_function next_send(void) {
    return send;
}
#endif

static void signal_checkpoint(const char *name) {
    const char *checkpoint = getenv(name);
    if (checkpoint == NULL) {
        return;
    }
    int descriptor = open(checkpoint, O_WRONLY | O_NONBLOCK);
    if (descriptor >= 0) {
        const char value = '1';
        (void)write(descriptor, &value, sizeof(value));
        close(descriptor);
    }
}

#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_START) \
    || defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP) \
    || defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_DISPOSITION)
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
#endif

#if defined(MCP_CONSOLE_INTERPOSE_FAILED_RECOVERY_STOP)
static int fail_process_info(
    int process_id,
    int flavor,
    uint64_t argument,
    void *buffer,
    int buffer_size
) {
    const char *trigger = getenv("MCP_CONSOLE_TEST_PROCESS_INFO_FAILURE_TRIGGER");
    if (flavor == PROC_PIDTBSDINFO
        && trigger != NULL
        && access(trigger, F_OK) == 0) {
        if (atomic_exchange(&failed_process_info, 1) == 0) {
            signal_checkpoint("MCP_CONSOLE_TEST_PROCESS_INFO_FAILURE");
        }
        errno = EIO;
        return 0;
    }
    return next_proc_pidinfo()(process_id, flavor, argument, buffer, buffer_size);
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_START) \
    || defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP) \
    || defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_DISPOSITION)
static int is_subcommand(const char *name) {
    int argc = *_NSGetArgc();
    char **argv = *_NSGetArgv();
    return argc > 1 && strcmp(argv[1], name) == 0;
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_START) \
    || defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP)
static int manager_control_descriptor(void) {
    const char *descriptor = getenv("MCP_CONSOLE_SANDBOX_MANAGER_FD");
    return descriptor == NULL ? STDIN_FILENO : atoi(descriptor);
}
#endif

__attribute__((constructor))
static void configure_interposer(void) {
#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_START) \
    || defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP) \
    || defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_DISPOSITION)
    if (!is_subcommand("sandbox-manager") && !is_subcommand("sandbox")) {
        unsetenv("DYLD_INSERT_LIBRARIES");
    }
#else
    unsetenv("DYLD_INSERT_LIBRARIES");
#endif
}

#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_START)
static ssize_t gate_manager_initialization(
    int descriptor,
    void *buffer,
    size_t length,
    int flags
) {
    if (descriptor == manager_control_descriptor()
        && getenv("MCP_CONSOLE_TEST_MANAGER_START") != NULL
        && is_subcommand("sandbox-manager")
        && atomic_exchange(&gated_manager_read, 1) == 0) {
        // The owner writes initialization only after spawning the gated root.
        // Peek one byte so the checkpoint implies that both children exist.
        char initialization;
        ssize_t count;
        do {
            count = recvfrom(
                descriptor,
                &initialization,
                sizeof(initialization),
                flags | MSG_PEEK,
                NULL,
                NULL
            );
        } while (count < 0 && errno == EINTR);
        if (count != sizeof(initialization)) {
            _exit(125);
        }
        signal_checkpoint("MCP_CONSOLE_TEST_MANAGER_START");
        wait_for_release("MCP_CONSOLE_TEST_MANAGER_RELEASE");
    }
    return recvfrom(descriptor, buffer, length, flags, NULL, NULL);
}

static ssize_t fail_target_gate_release(
    int descriptor,
    const void *buffer,
    size_t length,
    int flags
) {
    const unsigned char target_gate_release = 1;
    if (length == 1
        && *(const unsigned char *)buffer == target_gate_release
        && getenv("MCP_CONSOLE_TEST_TARGET_GATE_WRITE") != NULL
        && is_subcommand("sandbox")
        && atomic_exchange(&failed_target_release, 1) == 0) {
        retained_target_gate = dup(descriptor);
        if (retained_target_gate < 0) {
            _exit(125);
        }
        signal_checkpoint("MCP_CONSOLE_TEST_TARGET_GATE_WRITE");
        errno = EPIPE;
        return -1;
    }
    return next_send()(descriptor, buffer, length, flags);
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_DENIED_SIGKILL) \
    || defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP)
static int deny_first_sigkill(pid_t process_id, int number) {
    if (number == SIGKILL
        && getenv("MCP_CONSOLE_TEST_DENIED_SIGKILL") != NULL
#if defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP)
        && is_subcommand("sandbox")
#endif
        && atomic_exchange(&denied_sigkill, 1) == 0) {
        signal_checkpoint("MCP_CONSOLE_TEST_DENIED_SIGKILL");
        errno = EPERM;
        return -1;
    }
    kill_function kill_next = next_kill();
    if (kill_next == NULL) {
        errno = ENOSYS;
        return -1;
    }
    return kill_next(process_id, number);
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP)
static ssize_t delay_cleanup_acknowledgement(
    int descriptor,
    const void *buffer,
    size_t length,
    int flags
) {
    const unsigned char cleanup_complete = 5;
    const unsigned char preserve_temporary_directory = 4;
    if (descriptor == manager_control_descriptor()
        && length == 1
        && *(const unsigned char *)buffer == cleanup_complete
        && getenv("MCP_CONSOLE_TEST_LATE_CLEANUP") != NULL
        && is_subcommand("sandbox-manager")
        && atomic_exchange(&delayed_cleanup, 1) == 0) {
        unsigned char disposition;
        ssize_t count;
        do {
            count = recv(descriptor, &disposition, 1, MSG_PEEK);
        } while (count < 0 && errno == EINTR);
        if (count != 1 || disposition != preserve_temporary_directory) {
            _exit(125);
        }
        signal_checkpoint("MCP_CONSOLE_TEST_LATE_CLEANUP");
        if (getenv("MCP_CONSOLE_TEST_LATE_CLEANUP_RELEASE") != NULL) {
            wait_for_release("MCP_CONSOLE_TEST_LATE_CLEANUP_RELEASE");
        }
    }
    return sendto(descriptor, buffer, length, flags, NULL, 0);
}

static pid_t gate_root_reap(pid_t process_id, int *status, int options) {
    pid_t result = next_waitpid()(process_id, status, options);
    if (result > 0
        && options == 0
        && pthread_main_np() != 0
        && getenv("MCP_CONSOLE_TEST_ROOT_REAPED") != NULL
        && is_subcommand("sandbox")
        && atomic_exchange(&reaped_root, 1) == 0) {
        signal_checkpoint("MCP_CONSOLE_TEST_ROOT_REAPED");
        wait_for_release("MCP_CONSOLE_TEST_ROOT_REAP_RELEASE");
    }
    return result;
}

static int delay_late_recovery(
    int process_id,
    int flavor,
    uint64_t argument,
    void *buffer,
    int buffer_size
) {
    if (flavor == PROC_PIDTBSDINFO
        && atomic_load(&reaped_root) != 0
        && getenv("MCP_CONSOLE_TEST_LATE_RECOVERY") != NULL
        && is_subcommand("sandbox")
        && atomic_exchange(&delayed_late_recovery, 1) == 0) {
        signal_checkpoint("MCP_CONSOLE_TEST_LATE_RECOVERY");
        wait_for_release("MCP_CONSOLE_TEST_LATE_RECOVERY_RELEASE");
    }
    return next_proc_pidinfo()(process_id, flavor, argument, buffer, buffer_size);
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_DISPOSITION)
static ssize_t gate_retirement_disposition(
    int descriptor,
    const void *buffer,
    size_t length,
    int flags
) {
    const unsigned char remove_temporary_directory = 9;
    if (length == 1
        && *(const unsigned char *)buffer == remove_temporary_directory
        && getenv("MCP_CONSOLE_TEST_RETIREMENT_DISPOSITION") != NULL
        && is_subcommand("sandbox")
        && atomic_exchange(&gated_retirement_disposition, 1) == 0) {
        signal_checkpoint("MCP_CONSOLE_TEST_RETIREMENT_DISPOSITION");
        wait_for_release("MCP_CONSOLE_TEST_RETIREMENT_RELEASE");
    }
    return sendto(descriptor, buffer, length, flags, NULL, 0);
}
#endif

#define DYLD_INTERPOSE(replacement, replacee)                                  \
    __attribute__((used)) static struct {                                      \
        const void *replacement;                                               \
        const void *replacee;                                                  \
    } interpose_##replacee __attribute__((section("__DATA,__interpose"))) = {  \
        (const void *)(uintptr_t)&replacement,                                 \
        (const void *)(uintptr_t)&replacee,                                    \
    };

#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_START)
DYLD_INTERPOSE(gate_manager_initialization, recv)
DYLD_INTERPOSE(fail_target_gate_release, send)
#elif defined(MCP_CONSOLE_INTERPOSE_DENIED_SIGKILL)
DYLD_INTERPOSE(deny_first_sigkill, kill)
#elif defined(MCP_CONSOLE_INTERPOSE_FAILED_RECOVERY_STOP)
DYLD_INTERPOSE(fail_process_info, proc_pidinfo)
DYLD_INTERPOSE(fail_group_stop, killpg)
#elif defined(MCP_CONSOLE_INTERPOSE_FAILED_ROOT_OBSERVER)
DYLD_INTERPOSE(fail_root_observer, proc_pidinfo)
DYLD_INTERPOSE(fail_root_group_stop, kill)
#elif defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP)
DYLD_INTERPOSE(delay_cleanup_acknowledgement, send)
DYLD_INTERPOSE(deny_first_sigkill, kill)
DYLD_INTERPOSE(gate_root_reap, waitpid)
DYLD_INTERPOSE(delay_late_recovery, proc_pidinfo)
#elif defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_DISPOSITION)
DYLD_INTERPOSE(gate_retirement_disposition, send)
#endif
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
            definitions[behavior],
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


def _read_lines(stream: object, count: int, description: str) -> list[str]:
    descriptor = stream.fileno()  # type: ignore[attr-defined]
    output = bytearray()
    deadline = time.monotonic() + TIMEOUT
    with selectors.DefaultSelector() as selector:
        selector.register(descriptor, selectors.EVENT_READ)
        while output.count(b"\n") < count:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"timed out waiting for {description}"
            ready = selector.select(remaining)
            assert ready, f"timed out waiting for {description}"
            chunk = os.read(descriptor, 4096)
            assert chunk, f"sandbox closed before reporting {description}"
            output.extend(chunk)
    lines = output.decode("utf-8").splitlines()
    assert len(lines) == count, (description, lines)
    return lines


def _manager_pid(launcher_pid: int) -> int:
    deadline = time.monotonic() + TIMEOUT
    while True:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            check=True,
            timeout=TIMEOUT,
        )
        matches = []
        for line in result.stdout.splitlines():
            fields = line.strip().split(maxsplit=2)
            if (
                len(fields) == 3
                and int(fields[1]) == launcher_pid
                and "sandbox-manager" in fields[2]
            ):
                matches.append(int(fields[0]))
        assert len(matches) <= 1, (launcher_pid, matches)
        if matches:
            return matches[0]
        assert time.monotonic() < deadline, "sandbox manager did not start"
        time.sleep(0.01)


def _wait_for_private_startup_gate(identity: DarwinProcessIdentity) -> None:
    deadline = time.monotonic() + TIMEOUT
    while not darwin_process_waits_for_startup_release(identity):
        assert live_darwin_processes((identity,)), (
            "sandbox root exited before reaching its private startup gate"
        )
        assert time.monotonic() < deadline, (
            "sandbox target did not block at its private startup gate"
        )
        time.sleep(0.01)


@contextmanager
def _observe_process_exit(
    identity: DarwinProcessIdentity,
) -> Iterator[select.kqueue]:
    events = select.kqueue()
    events.control(
        [
            select.kevent(
                identity[0],
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                fflags=select.KQ_NOTE_EXIT,
            )
        ],
        0,
        0,
    )
    try:
        yield events
    finally:
        events.close()


def _thread_count(identity: DarwinProcessIdentity) -> int | None:
    if not live_darwin_processes((identity,)):
        return None
    result = subprocess.run(
        ["/bin/ps", "-M", "-p", str(identity[0])],
        capture_output=True,
        text=True,
        check=True,
        timeout=TIMEOUT,
    )
    if not live_darwin_processes((identity,)):
        return None
    lines = result.stdout.splitlines()
    assert lines, result.stdout
    return len(lines) - 1


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


def _wait_for_manager_readiness(lifetime: _SandboxLifetime) -> None:
    # SandboxManager starts its launcher-side monitor thread only after the
    # manager's readiness byte has been received. This is a causal commitment
    # checkpoint, unlike sleeping after discovering the manager process.
    deadline = time.monotonic() + TIMEOUT
    while True:
        thread_count = _thread_count(lifetime.launcher)
        assert thread_count is not None, (
            "sandbox launcher exited before manager readiness"
        )
        assert live_darwin_processes((lifetime.manager,)), (
            "sandbox manager exited before readiness"
        )
        if thread_count >= 2:
            return
        assert time.monotonic() < deadline, "sandbox manager did not become ready"
        time.sleep(0.01)


def _start_lifetime(
    binary: Path,
    environment: dict[str, str] | None = None,
    *,
    detached: bool = True,
) -> _SandboxLifetime:
    # The detached child leaves the root's session, so cleanup must come from
    # exact descendant observation rather than an inherited process group.
    # fmt: python
    script = code(rf"""
        import os
        import subprocess
        import sys

        child = subprocess.Popen(
            ["/bin/sleep", "60"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session={detached!r},
        )
        print(os.getpid())
        print(child.pid)
        print(os.environ["TMPDIR"])
        sys.stdout.flush()
        if sys.stdin.readline() == "exit\n":
            raise SystemExit(23)
        raise SystemExit(24)
        """)
    arguments = ("sandbox", "--", "python", "-c", script)
    process = subprocess.Popen(
        [binary, *arguments],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    identities: list[DarwinProcessIdentity] = []
    temporary_directory: Path | None = None
    try:
        root_pid, descendant_pid, temporary_directory_text = _read_lines(
            process.stdout,
            3,
            "the sandbox root, detached descendant, and temporary directory",
        )
        temporary_directory = Path(temporary_directory_text)
        root = capture_darwin_process_identity(int(root_pid))
        identities.append(root)
        descendant = capture_darwin_process_identity(int(descendant_pid))
        identities.append(descendant)
        launcher = capture_darwin_process_identity(process.pid)
        if detached:
            assert os.getsid(descendant[0]) != os.getsid(root[0]), (
                "sandbox descendant did not leave the root session"
            )
        else:
            assert os.getpgid(descendant[0]) == os.getpgid(root[0]), (
                "sandbox descendant did not remain in the root process group"
            )
        manager = capture_darwin_process_identity(_manager_pid(process.pid))
        identities.append(manager)
        lifetime = _SandboxLifetime(
            process=process,
            arguments=arguments,
            launcher=launcher,
            root=root,
            descendant=descendant,
            manager=manager,
            temporary_directory=temporary_directory,
        )
        _wait_for_manager_readiness(lifetime)
        return lifetime
    except BaseException as error:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=TIMEOUT)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stderr.fileno(), selectors.EVENT_READ)
            stderr_ready = selector.select(0)
        stderr = (
            os.read(process.stderr.fileno(), 4096).decode("utf-8", errors="replace")
            if stderr_ready
            else ""
        )
        error.add_note(
            f"sandbox returncode after setup failure: {process.returncode}\n"
            f"sandbox stderr:\n{stderr}"
        )
        kill_darwin_processes(identities)
        if temporary_directory is not None:
            shutil.rmtree(temporary_directory, ignore_errors=True)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
        raise


def _wait_for_cleanup(lifetime: _SandboxLifetime, timeout: float = 5) -> list[int]:
    identities = (lifetime.root, lifetime.descendant, lifetime.manager)
    deadline = time.monotonic() + timeout
    survivors = live_darwin_processes(identities)
    while (
        survivors or lifetime.temporary_directory.exists()
    ) and time.monotonic() < deadline:
        time.sleep(0.01)
        survivors = live_darwin_processes(identities)
    return live_darwin_processes(identities)


def _wait_for_process_exit(
    identities: tuple[DarwinProcessIdentity, ...],
    description: str,
    timeout: float = 5,
) -> list[int]:
    deadline = time.monotonic() + timeout
    survivors = live_darwin_processes(identities)
    while survivors and time.monotonic() < deadline:
        time.sleep(0.01)
        survivors = live_darwin_processes(identities)
    assert survivors == [], f"{description}: {survivors}"
    return survivors


def _cleanup(lifetime: _SandboxLifetime) -> None:
    if lifetime.process.poll() is None:
        lifetime.process.kill()
        lifetime.process.wait(timeout=TIMEOUT)
    kill_darwin_processes((lifetime.root, lifetime.descendant, lifetime.manager))
    shutil.rmtree(lifetime.temporary_directory, ignore_errors=True)
    for stream in (
        lifetime.process.stdin,
        lifetime.process.stdout,
        lifetime.process.stderr,
    ):
        if not stream.closed:
            stream.close()


def _command_record(lifetime: _SandboxLifetime) -> dict[str, object]:
    return {
        "command": _command(*lifetime.arguments),
        "stdout": "<sandbox root pid>\n<detached descendant pid>\n<sandbox temp>\n",
    }


def test_retires_every_processx_pipeline_stage(binary: Path) -> Transcript:
    # processx 3.9 pipelines contain regular process objects. On Unix, each
    # stage creates its own session, so no stage is contained by the root group.
    # fmt: r
    script = code(r"""
        pipeline <- processx::pipeline$new(
          list(
            c("/bin/sleep", "60"),
            c("/bin/cat")
          ),
          stdout = "|",
          stderr = "|",
          cleanup = FALSE
        )
        writeLines(as.character(pipeline$get_pids()))
        flush.console()
        stopifnot(identical(readLines("stdin", n = 1L), "exit"))
        quit(save = "no", status = 23L, runLast = FALSE)
        """)
    arguments = ("sandbox", "--", "Rscript", "--vanilla", "-e", script)
    process = subprocess.Popen(
        [binary, *arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    identities: list[DarwinProcessIdentity] = []
    try:
        pids = [
            int(line)
            for line in _read_lines(
                process.stdout,
                2,
                "the processx pipeline stage PIDs",
            )
        ]
        for pid in pids:
            identities.append(capture_darwin_process_identity(pid))

        process.stdin.write(b"exit\n")
        process.stdin.close()
        returncode = process.wait(timeout=TIMEOUT)
        stderr = process.stderr.read().decode("utf-8")
        survivors = kill_darwin_processes(identities)

        assert returncode == 23, returncode
        assert stderr == "", stderr
        assert len(identities) == 2, identities
        assert survivors == [], f"processx pipeline stages survived: {survivors}"
    finally:
        if process.poll() is None:
            if not process.stdin.closed:
                try:
                    process.stdin.write(b"exit\n")
                    process.stdin.close()
                except BrokenPipeError:
                    pass
            try:
                process.wait(timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=TIMEOUT)
        kill_darwin_processes(identities)
        if not process.stdin.closed:
            process.stdin.close()
        process.stdout.close()
        process.stderr.close()

    return [
        {
            "command": _command(*arguments),
            "exit_code": returncode,
            "stdout": "<pipeline stage pid>\n<pipeline stage pid>\n",
        }
    ]


def test_target_waits_for_manager_adoption(binary: Path) -> Transcript:
    # The manager's first control read is held before it can consume
    # initialization or adopt TMPDIR. The exact root must already be blocked on
    # its private gate.
    # fmt: python
    script = code(r"""
        import os

        temporary_directory = os.environ["TMPDIR"]
        os.rmdir(temporary_directory)
        raise SystemExit(23)
        """)
    arguments = ("sandbox", "--", "python", "-c", script)

    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_directory = Path(temporary_directory)
        manager_started = FifoCheckpoint(fixture_directory / "manager-started")
        manager_release = FifoCheckpoint(fixture_directory / "manager-release")
        environment = os.environ.copy()
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_supervision_interposer(fixture_directory, "manager-start")
        )
        environment["MCP_CONSOLE_TEST_MANAGER_START"] = str(manager_started.path)
        environment["MCP_CONSOLE_TEST_MANAGER_RELEASE"] = str(manager_release.path)
        environment["TMPDIR"] = str(fixture_directory)

        process = subprocess.Popen(
            [binary, *arguments],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        identities: list[DarwinProcessIdentity] = []
        manager_released = False
        sandbox_temporary_directory: Path | None = None
        try:
            manager_started.wait("manager startup before temporary-directory adoption")
            launcher = capture_darwin_process_identity(process.pid)
            children = darwin_child_process_identities(launcher)
            assert len(children) == 2, children
            deadline = time.monotonic() + TIMEOUT
            gated: list[DarwinProcessIdentity] = []
            while not gated:
                gated = [
                    child
                    for child in children
                    if darwin_process_waits_for_startup_release(child)
                ]
                assert len(gated) <= 1, (children, gated)
                if gated:
                    break
                assert live_darwin_processes(children) == [
                    child[0] for child in children
                ], children
                assert time.monotonic() < deadline, (
                    "sandbox root did not reach its private startup gate"
                )
                time.sleep(0.01)
            assert len(gated) == 1, (children, gated)
            root = gated[0]
            manager = next(child for child in children if child != root)
            identities.extend((root, manager))
            temporary_directories = list(
                fixture_directory.glob(f"mcp-console-tmp-{process.pid}-*")
            )
            assert len(temporary_directories) == 1, temporary_directories
            sandbox_temporary_directory = temporary_directories[0]
            assert signal_darwin_process(root, signal.SIGCONT), (
                "sandbox target exited before the gate-bypass probe"
            )
            _wait_for_private_startup_gate(root)
            assert sandbox_temporary_directory.exists(), (
                "SIGCONT released the target before manager adoption"
            )

            manager_release.release()
            manager_released = True
            returncode = process.wait(timeout=TIMEOUT)
            stdout = process.stdout.read().decode("utf-8")
            stderr = process.stderr.read().decode("utf-8")

            assert returncode == 23, returncode
            assert stdout == "", stdout
            assert stderr == "", stderr
            assert not sandbox_temporary_directory.exists(), (
                "sandbox target did not remove its temporary directory"
            )
            return [
                {
                    "command": _command(*arguments),
                    "stdout": "",
                },
                {
                    "manager_checkpoint": "before temporary-directory adoption",
                    "verified_root_state": "blocked on private startup gate",
                    "gate_bypass_probe": "SIGCONT",
                },
                {
                    "launcher_returncode": returncode,
                    "verified_target": "removed sandbox temp after manager readiness",
                },
            ]
        finally:
            if not manager_released:
                manager_release.release()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=TIMEOUT)
            kill_darwin_processes(identities)
            if sandbox_temporary_directory is not None:
                shutil.rmtree(sandbox_temporary_directory, ignore_errors=True)
            for stream in (process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()
            manager_started.close()
            manager_release.close()


def test_preserves_root_signal_status_when_startup_gate_breaks(
    binary: Path,
) -> Transcript:
    arguments = ("sandbox", "--", "/bin/sleep", "60")
    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_directory = Path(temporary_directory)
        manager_started = FifoCheckpoint(fixture_directory / "manager-started")
        manager_release = FifoCheckpoint(fixture_directory / "manager-release")
        target_gate_write = FifoCheckpoint(fixture_directory / "target-gate-write")
        environment = os.environ.copy()
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_supervision_interposer(fixture_directory, "manager-start")
        )
        environment["MCP_CONSOLE_TEST_MANAGER_START"] = str(manager_started.path)
        environment["MCP_CONSOLE_TEST_MANAGER_RELEASE"] = str(manager_release.path)
        environment["MCP_CONSOLE_TEST_TARGET_GATE_WRITE"] = str(target_gate_write.path)
        environment["TMPDIR"] = str(fixture_directory)

        process = subprocess.Popen(
            [binary, *arguments],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        identities: list[DarwinProcessIdentity] = []
        manager_released = False
        sandbox_temporary_directory: Path | None = None
        try:
            manager_started.wait("manager startup before target release")
            launcher = capture_darwin_process_identity(process.pid)
            children = darwin_child_process_identities(launcher)
            assert len(children) == 2, children
            manager_pid = _manager_pid(process.pid)
            manager = next(child for child in children if child[0] == manager_pid)
            root = next(child for child in children if child != manager)
            _wait_for_private_startup_gate(root)
            identities.extend((root, manager))
            temporary_directories = list(
                fixture_directory.glob(f"mcp-console-tmp-{process.pid}-*")
            )
            assert len(temporary_directories) == 1, temporary_directories
            sandbox_temporary_directory = temporary_directories[0]

            with _observe_process_exit(root) as events:
                manager_release.release()
                manager_released = True
                target_gate_write.wait("target gate EPIPE before root exit")
                _wait_for_private_startup_gate(root)
                assert signal_darwin_process(root, signal.SIGINT), (
                    "sandbox root exited before signal injection"
                )
                assert events.control(None, 1, TIMEOUT), (
                    "sandbox root did not exit after SIGINT"
                )
            returncode = process.wait(timeout=TIMEOUT)
            stdout = process.stdout.read().decode("utf-8")
            stderr = process.stderr.read().decode("utf-8")

            assert returncode == 128 + signal.SIGINT, returncode
            assert stdout == "", stdout
            assert stderr == "", stderr
            assert live_darwin_processes(tuple(identities)) == [], (
                "sandbox root or manager survived startup failure cleanup"
            )
            assert not sandbox_temporary_directory.exists(), (
                "startup signal left the sandbox temporary directory"
            )
            return [
                {"command": _command(*arguments)},
                {
                    "startup_gate_failure": "EPIPE before root exit",
                    "root_signal": "SIGINT while target remains gated",
                    "launcher_returncode": returncode,
                    "verified_cleanup": "sandbox root, manager, and temp",
                },
            ]
        finally:
            if not manager_released:
                manager_release.release()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=TIMEOUT)
            kill_darwin_processes(identities)
            if sandbox_temporary_directory is not None:
                shutil.rmtree(sandbox_temporary_directory, ignore_errors=True)
            process.stdout.close()
            process.stderr.close()
            manager_started.close()
            manager_release.close()
            target_gate_write.close()


def test_launcher_crash_retires_the_sandbox_lifetime(binary: Path) -> Transcript:
    lifetime = _start_lifetime(binary)
    try:
        lifetime.process.kill()
        returncode = lifetime.process.wait(timeout=TIMEOUT)
        survivors = _wait_for_cleanup(lifetime)
        stderr = lifetime.process.stderr.read().decode("utf-8")

        assert returncode == -signal.SIGKILL, returncode
        assert stderr == "", stderr
        assert survivors == [], f"launcher crash leaked sandbox processes: {survivors}"
        assert not lifetime.temporary_directory.exists(), (
            "launcher crash leaked the sandbox temporary directory"
        )
        return [
            _command_record(lifetime),
            {
                "launcher_signal": "SIGKILL",
                "launcher_returncode": returncode,
                "verified_cleanup": "sandbox root, detached descendant, manager, and temp",
            },
        ]
    finally:
        _cleanup(lifetime)


def test_manager_crash_retires_the_sandbox_lifetime(binary: Path) -> Transcript:
    lifetime = _start_lifetime(binary)
    try:
        assert signal_darwin_process(lifetime.manager, signal.SIGKILL), (
            "manager exited before crash injection"
        )
        returncode = lifetime.process.wait(timeout=TIMEOUT)
        stderr = lifetime.process.stderr.read().decode("utf-8")
        survivors = _wait_for_cleanup(lifetime)

        assert returncode == 128 + signal.SIGKILL, returncode
        assert stderr == "", stderr
        assert survivors == [], f"manager crash leaked sandbox processes: {survivors}"
        assert not lifetime.temporary_directory.exists(), (
            "manager crash leaked the sandbox temporary directory"
        )
        return [
            _command_record(lifetime),
            {
                "manager_signal": "SIGKILL",
                "launcher_returncode": returncode,
                "verified_cleanup": "sandbox root, detached descendant, manager, and temp",
            },
        ]
    finally:
        _cleanup(lifetime)


def test_manager_crash_with_zombie_root_stops_pinned_group(
    binary: Path,
) -> Transcript:
    lifetime = _start_lifetime(binary, detached=False)
    try:
        assert signal_darwin_process(lifetime.manager, signal.SIGSTOP), (
            "manager exited before stop injection"
        )
        _wait_for_process_state(lifetime.manager, "T", "sandbox manager")

        with _observe_process_exit(lifetime.root) as events:
            lifetime.process.stdin.write(b"exit\n")
            lifetime.process.stdin.close()
            assert events.control(None, 1, TIMEOUT), (
                "sandbox root did not exit while manager was stopped"
            )
        _wait_for_process_state(lifetime.root, "Z", "sandbox root")

        assert signal_darwin_process(lifetime.manager, signal.SIGKILL), (
            "manager exited before crash injection"
        )
        returncode = lifetime.process.wait(timeout=TIMEOUT)
        stderr = lifetime.process.stderr.read().decode("utf-8")
        normalized_stderr = stderr.replace(
            str(lifetime.root[0]),
            "<sandbox root pid>",
        )
        _wait_for_process_exit(
            (lifetime.root, lifetime.descendant, lifetime.manager),
            "sandbox process survived zombie-root manager recovery",
        )

        assert returncode == 1, returncode
        assert "sandbox root" in stderr, stderr
        assert "exited before fallback supervision" in stderr, stderr
        assert lifetime.temporary_directory.exists(), (
            "zombie-root recovery removed the sandbox temporary directory"
        )
        return [
            {
                "command": _command(*lifetime.arguments),
                "stdout": (
                    "<sandbox root pid>\n<same-group descendant pid>\n<sandbox temp>\n"
                ),
                "stderr": normalized_stderr,
            },
            {
                "manager_signal": "SIGSTOP then SIGKILL",
                "verified_root_state": "waitable zombie during recovery",
                "launcher_returncode": returncode,
                "verified_cleanup": "sandbox root, same-group descendant, and manager",
                "verified_preservation": "sandbox temp",
            },
        ]
    finally:
        _cleanup(lifetime)


def test_manager_recovery_failure_wakes_launcher(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_directory = Path(temporary_directory)
        denied_sigkill = FifoCheckpoint(fixture_directory / "denied-sigkill")
        environment = os.environ.copy()
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_supervision_interposer(fixture_directory, "denied-sigkill")
        )
        environment["MCP_CONSOLE_TEST_DENIED_SIGKILL"] = str(denied_sigkill.path)
        lifetime = _start_lifetime(binary, environment)
        try:
            assert signal_darwin_process(lifetime.manager, signal.SIGKILL), (
                "manager exited before crash injection"
            )
            denied_sigkill.wait("launcher manager-recovery signal denial")
            returncode = lifetime.process.wait(timeout=TIMEOUT)
            stderr = lifetime.process.stderr.read().decode("utf-8")
            normalized_stderr = stderr
            for identity in (lifetime.root, lifetime.descendant):
                normalized_stderr = normalized_stderr.replace(
                    str(identity[0]),
                    "<sandbox process pid>",
                )
            _wait_for_process_exit(
                (lifetime.root, lifetime.descendant, lifetime.manager),
                "sandbox processes survived manager recovery failure",
            )

            assert returncode == 1, returncode
            assert "manager recovery failed" in stderr, stderr
            assert "Operation not permitted" in stderr, stderr
            assert lifetime.temporary_directory.exists(), (
                "manager recovery failure removed the sandbox temporary directory"
            )
            command = _command_record(lifetime)
            command["stderr"] = normalized_stderr
            return [
                command,
                {
                    "manager_signal": "SIGKILL",
                    "manager_recovery_signal": "EPERM",
                    "launcher_returncode": returncode,
                    "verified_cleanup": "sandbox root, detached descendant, and manager",
                    "verified_preservation": "sandbox temp",
                },
            ]
        finally:
            denied_sigkill.close()
            _cleanup(lifetime)


def test_manager_recovery_inspection_failure_stops_root(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_directory = Path(temporary_directory)
        inspection_failed = FifoCheckpoint(fixture_directory / "inspection-failed")
        group_stop_failed = FifoCheckpoint(fixture_directory / "group-stop-failed")
        failure_trigger = fixture_directory / "fail-process-info"
        environment = os.environ.copy()
        environment.update(
            {
                "DYLD_INSERT_LIBRARIES": str(
                    _build_supervision_interposer(
                        fixture_directory,
                        "failed-recovery-stop",
                    )
                ),
                "MCP_CONSOLE_TEST_PROCESS_INFO_FAILURE": str(inspection_failed.path),
                "MCP_CONSOLE_TEST_PROCESS_INFO_FAILURE_TRIGGER": str(failure_trigger),
                "MCP_CONSOLE_TEST_GROUP_STOP_FAILURE": str(group_stop_failed.path),
            }
        )
        lifetime = _start_lifetime(binary, environment)
        try:
            failure_trigger.touch()
            assert signal_darwin_process(lifetime.manager, signal.SIGKILL), (
                "manager exited before crash injection"
            )
            inspection_failed.wait("launcher root-inspection failure")
            group_stop_failed.wait("launcher pinned-group stop failure")
            returncode = lifetime.process.wait(timeout=TIMEOUT)
            stderr = lifetime.process.stderr.read().decode("utf-8")
            normalized_stderr = stderr.replace(
                str(lifetime.root[0]),
                "<sandbox root pid>",
            )
            assert returncode == 1, returncode
            assert "manager recovery failed" in stderr, stderr
            assert "failed to inspect sandbox process" in stderr, stderr
            assert "failed to stop sandbox process group" in stderr, stderr
            assert "Input/output error" in stderr, stderr
            assert live_darwin_processes((lifetime.root, lifetime.manager)) == [], (
                "sandbox root or manager survived failed recovery inspection"
            )
            assert live_darwin_processes((lifetime.descendant,)) == [
                lifetime.descendant[0]
            ], "failed recovery unexpectedly claimed detached-descendant cleanup"
            assert lifetime.temporary_directory.exists(), (
                "manager inspection failure removed the sandbox temporary directory"
            )
            command = _command_record(lifetime)
            command["stderr"] = normalized_stderr
            return [
                command,
                {
                    "manager_signal": "SIGKILL",
                    "manager_recovery_inspection": "EIO",
                    "manager_recovery_group_stop": "EIO",
                    "launcher_returncode": returncode,
                    "verified_bounded_return": "after direct pinned-root termination",
                    "verified_cleanup": "sandbox root and manager",
                    "verified_preservation": "detached descendant and sandbox temp",
                },
            ]
        finally:
            inspection_failed.close()
            group_stop_failed.close()
            _cleanup(lifetime)


def test_root_observer_failure_reports_group_cleanup_failure(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        inspection_failed = FifoCheckpoint(temporary / "inspection-failed")
        group_stop_failed = FifoCheckpoint(temporary / "group-stop-failed")
        environment = os.environ.copy()
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_supervision_interposer(temporary, "failed-root-observer")
        )
        environment["MCP_CONSOLE_TEST_PROCESS_INFO_FAILURE"] = str(
            inspection_failed.path
        )
        environment["MCP_CONSOLE_TEST_GROUP_STOP_FAILURE"] = str(group_stop_failed.path)
        try:
            result = subprocess.run(
                [binary, "sandbox", "--", "/bin/sleep", "60"],
                env=environment,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
            )
            inspection_failed.wait("sandbox root-observer failure")
            group_stop_failed.wait("sandbox root-group stop failure")
            stderr = re.sub(
                r"sandbox process \d+", "sandbox process <pid>", result.stderr
            )
            assert result.returncode == 1, result.returncode
            assert "failed to inspect sandbox process" in stderr, stderr
            assert "process-group termination also failed" in stderr, stderr
            return [
                {
                    "command": _command("sandbox", "--", "/bin/sleep", "60"),
                    "stderr": stderr,
                }
            ]
        finally:
            inspection_failed.close()
            group_stop_failed.close()


def test_cleanup_timeout_preserves_temporary_directory(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_directory = Path(temporary_directory)
        late_cleanup = FifoCheckpoint(fixture_directory / "late-cleanup")
        environment = os.environ.copy()
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_supervision_interposer(fixture_directory, "late-cleanup")
        )
        environment["MCP_CONSOLE_TEST_LATE_CLEANUP"] = str(late_cleanup.path)
        lifetime = _start_lifetime(binary, environment)
        try:
            lifetime.process.stdin.write(b"exit\n")
            lifetime.process.stdin.close()
            late_cleanup.wait("cleanup acknowledgement after launcher timeout")
            returncode = lifetime.process.wait(timeout=TIMEOUT)
            stderr = lifetime.process.stderr.read().decode("utf-8")
            _wait_for_process_exit(
                (lifetime.root, lifetime.descendant, lifetime.manager),
                "sandbox processes survived delayed cleanup acknowledgement",
            )

            assert returncode == 23, returncode
            assert stderr == "", stderr
            assert lifetime.temporary_directory.exists(), (
                "cleanup timeout removed the sandbox temporary directory"
            )
            return [
                _command_record(lifetime),
                {
                    "manager_cleanup": "acknowledgement delayed past launcher timeout",
                    "launcher_returncode": returncode,
                    "verified_cleanup": "sandbox root, detached descendant, and manager",
                    "verified_preservation": "sandbox temp",
                },
            ]
        finally:
            late_cleanup.close()
            _cleanup(lifetime)


def test_manager_stop_failure_remains_bounded(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_directory = Path(temporary_directory)
        late_cleanup = FifoCheckpoint(fixture_directory / "late-cleanup")
        late_cleanup_release = FifoCheckpoint(
            fixture_directory / "late-cleanup-release"
        )
        denied_sigkill = FifoCheckpoint(fixture_directory / "denied-sigkill")
        root_reaped = FifoCheckpoint(fixture_directory / "root-reaped")
        root_reap_release = FifoCheckpoint(fixture_directory / "root-reap-release")
        late_recovery = FifoCheckpoint(fixture_directory / "late-recovery")
        late_recovery_release = FifoCheckpoint(
            fixture_directory / "late-recovery-release"
        )
        environment = os.environ.copy()
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_supervision_interposer(
                fixture_directory,
                "late-cleanup",
            )
        )
        environment["MCP_CONSOLE_TEST_LATE_CLEANUP"] = str(late_cleanup.path)
        environment["MCP_CONSOLE_TEST_LATE_CLEANUP_RELEASE"] = str(
            late_cleanup_release.path
        )
        environment["MCP_CONSOLE_TEST_DENIED_SIGKILL"] = str(denied_sigkill.path)
        environment["MCP_CONSOLE_TEST_ROOT_REAPED"] = str(root_reaped.path)
        environment["MCP_CONSOLE_TEST_ROOT_REAP_RELEASE"] = str(root_reap_release.path)
        environment["MCP_CONSOLE_TEST_LATE_RECOVERY"] = str(late_recovery.path)
        environment["MCP_CONSOLE_TEST_LATE_RECOVERY_RELEASE"] = str(
            late_recovery_release.path
        )
        lifetime = _start_lifetime(binary, environment)
        root_reap_released = False
        try:
            lifetime.process.stdin.write(b"exit\n")
            lifetime.process.stdin.close()
            late_cleanup.wait("cleanup acknowledgement after launcher timeout")
            denied_sigkill.wait("launcher manager-stop signal denial")
            root_reaped.wait("root reap after the second manager deadline")
            assert signal_darwin_process(lifetime.manager, signal.SIGKILL), (
                "manager exited before late monitor shutdown"
            )

            deadline = time.monotonic() + TIMEOUT
            while True:
                thread_count = _thread_count(lifetime.launcher)
                if thread_count == 1:
                    break
                remaining = deadline - time.monotonic()
                assert remaining > 0, (
                    "detached sandbox manager monitor did not stop: "
                    f"launcher has {thread_count} threads"
                )
                readable, _, _ = select.select(
                    [late_recovery.descriptor],
                    [],
                    [],
                    min(0.05, remaining),
                )
                assert not readable, (
                    "detached monitor inspected the root after its PID pin was released"
                )

            root_reap_release.release()
            root_reap_released = True
            returncode = lifetime.process.wait(timeout=TIMEOUT)
            stderr = lifetime.process.stderr.read().decode("utf-8")
            normalized_stderr = stderr.replace(
                str(lifetime.manager[0]),
                "<sandbox manager pid>",
            )

            assert returncode == 1, returncode
            assert "timed out waiting for sandbox manager cleanup" in stderr, stderr
            assert "failed to stop sandbox manager" in stderr, stderr
            assert "Operation not permitted" in stderr, stderr
            _wait_for_process_exit(
                (lifetime.root, lifetime.descendant, lifetime.manager),
                "sandbox processes survived late manager shutdown",
            )
            assert lifetime.temporary_directory.exists(), (
                "manager-stop failure removed the sandbox temporary directory"
            )
            command = _command_record(lifetime)
            command["stderr"] = normalized_stderr
            return [
                command,
                {
                    "manager_cleanup": "acknowledgement held past both owner deadlines",
                    "manager_stop_signal": "EPERM",
                    "launcher_returncode": returncode,
                    "verified_bounded_return": "within the recovery deadline",
                    "verified_detached_monitor": "no recovery after root reap",
                    "verified_cleanup": "sandbox root, detached descendant, and manager",
                    "verified_preservation": "sandbox temp",
                },
            ]
        finally:
            late_cleanup_release.release()
            late_recovery_release.release()
            if not root_reap_released:
                root_reap_release.release()
            late_cleanup.close()
            late_cleanup_release.close()
            denied_sigkill.close()
            root_reaped.close()
            root_reap_release.close()
            late_recovery.close()
            late_recovery_release.close()
            _cleanup(lifetime)


def test_launcher_crash_during_retirement_preserves_temporary_directory(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_directory = Path(temporary_directory)
        disposition = FifoCheckpoint(fixture_directory / "retirement-disposition")
        disposition_release = FifoCheckpoint(
            fixture_directory / "retirement-disposition-release"
        )
        environment = os.environ.copy()
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_supervision_interposer(
                fixture_directory,
                "retirement-disposition",
            )
        )
        environment["MCP_CONSOLE_TEST_RETIREMENT_DISPOSITION"] = str(disposition.path)
        environment["MCP_CONSOLE_TEST_RETIREMENT_RELEASE"] = str(
            disposition_release.path
        )
        lifetime = _start_lifetime(binary, environment)
        try:
            lifetime.process.stdin.write(b"exit\n")
            lifetime.process.stdin.close()
            disposition.wait("final directory disposition after cleanup")
            _wait_for_process_exit(
                (lifetime.descendant,),
                "detached descendant survived retirement",
            )
            assert live_darwin_processes((lifetime.manager,)) == [
                lifetime.manager[0]
            ], "manager exited before final directory disposition"
            assert lifetime.temporary_directory.exists(), (
                "temporary directory disappeared before final disposition"
            )

            assert signal_darwin_process(lifetime.launcher, signal.SIGKILL), (
                "launcher exited before crash injection"
            )
            returncode = lifetime.process.wait(timeout=TIMEOUT)
            stderr = lifetime.process.stderr.read().decode("utf-8")
            _wait_for_process_exit(
                (lifetime.root, lifetime.descendant, lifetime.manager),
                "sandbox processes survived owner loss during retirement",
            )

            assert returncode == -signal.SIGKILL, returncode
            assert stderr == "", stderr
            assert lifetime.temporary_directory.exists(), (
                "manager removed the temporary directory after retirement began"
            )
            return [
                _command_record(lifetime),
                {
                    "manager_checkpoint": "cleanup complete",
                    "verified_manager_state": "waiting for directory disposition",
                    "verified_cleanup": "detached descendant",
                },
                {
                    "launcher_signal": "SIGKILL",
                    "launcher_returncode": returncode,
                    "verified_cleanup": "sandbox root and manager",
                    "verified_preservation": "sandbox temp",
                },
            ]
        finally:
            disposition_release.release()
            disposition.close()
            disposition_release.close()
            _cleanup(lifetime)


if __name__ == "__main__":
    run_this_suite(__file__)
