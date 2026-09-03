import fcntl
import os
import pty
import re
import select
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
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
    build_manager_interposer,
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


def _start_with_controlling_terminal(
    arguments: list[str | Path],
    environment: dict[str, str],
) -> tuple[subprocess.Popen[bytes], int]:
    master, slave = pty.openpty()

    def attach_controlling_terminal() -> None:
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        os.tcsetpgrp(slave, os.getpid())

    process = subprocess.Popen(
        arguments,
        env=environment,
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=attach_controlling_terminal,
    )
    os.close(slave)
    assert process.stdout is not None
    assert process.stderr is not None
    return process, master


def _build_supervision_interposer(directory: Path, behavior: str) -> Path:
    definitions = {
        "manager-start": "-DMCP_CONSOLE_INTERPOSE_MANAGER_START",
        "manager-stop-failure": "-DMCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE",
        "denied-sigkill": "-DMCP_CONSOLE_INTERPOSE_DENIED_SIGKILL",
        "failed-recovery-stop": "-DMCP_CONSOLE_INTERPOSE_FAILED_RECOVERY_STOP",
        "failed-root-observer": "-DMCP_CONSOLE_INTERPOSE_FAILED_ROOT_OBSERVER",
        "late-cleanup": "-DMCP_CONSOLE_INTERPOSE_LATE_CLEANUP",
        "retirement-cleanup": "-DMCP_CONSOLE_INTERPOSE_RETIREMENT_CLEANUP",
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
#include <sys/event.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#if defined(MCP_CONSOLE_INTERPOSE_DENIED_SIGKILL) \
    || defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP)
static _Atomic int denied_sigkill = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE)
static _Atomic int manager_group_stop_started = 0;
static _Atomic int manager_root_stop_reported = 0;
static _Atomic pid_t manager_observed_root = 0;
static _Atomic pid_t manager_observed_descendant = 0;
static _Atomic int manager_descendant_observed_reported = 0;
static _Atomic int manager_descendant_stop_reported = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP)
static _Atomic int delayed_late_recovery = 0;
static _Atomic int reaped_root = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP) \
    || defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_CLEANUP)
static _Atomic int gated_manager_group_cleanup = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_START)
static _Atomic int gated_manager_read = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_FAILED_RECOVERY_STOP)
static _Atomic int failed_process_info = 0;
static _Atomic int failed_group_stop = 0;
static _Atomic int gated_recovery_root_stop = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_FAILED_ROOT_OBSERVER)
static _Atomic int root_exit_watch_registered = 0;
static _Atomic int failed_root_identity_recheck = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_DENIED_SIGKILL) \
    || defined(MCP_CONSOLE_INTERPOSE_FAILED_RECOVERY_STOP) \
    || defined(MCP_CONSOLE_INTERPOSE_FAILED_ROOT_OBSERVER) \
    || defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP) \
    || defined(MCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE)
typedef int (*kill_function)(pid_t, int);

static kill_function next_kill(void) {
    return kill;
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE) \
    || defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP) \
    || defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_CLEANUP)
typedef int (*killpg_function)(pid_t, int);

static killpg_function next_killpg(void) {
    return killpg;
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

#if defined(MCP_CONSOLE_INTERPOSE_FAILED_ROOT_OBSERVER) \
    || defined(MCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE)
typedef int (*kevent_function)(
    int,
    const struct kevent *,
    int,
    struct kevent *,
    int,
    const struct timespec *
);

static kevent_function next_kevent(void) {
    return kevent;
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_FAILED_ROOT_OBSERVER)
static void signal_checkpoint(const char *name);

static int arm_root_identity_recheck(
    int descriptor,
    const struct kevent *changes,
    int change_count,
    struct kevent *events,
    int event_count,
    const struct timespec *timeout
) {
    int result = next_kevent()(
        descriptor,
        changes,
        change_count,
        events,
        event_count,
        timeout
    );
    if (result >= 0
        && change_count == 1
        && changes != NULL
        && changes[0].filter == EVFILT_PROC
        && (changes[0].flags & EV_ADD) != 0
        && (changes[0].fflags & NOTE_EXIT) != 0) {
        atomic_store(&root_exit_watch_registered, 1);
    }
    return result;
}

static int fail_root_observer(
    int process_id,
    int flavor,
    uint64_t argument,
    void *buffer,
    int buffer_size
) {
    if (flavor == PROC_PIDTBSDINFO
        && atomic_load(&root_exit_watch_registered) != 0
        && atomic_exchange(&failed_root_identity_recheck, 1) == 0) {
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
        atomic_store(&failed_group_stop, 1);
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
    || defined(MCP_CONSOLE_INTERPOSE_FAILED_RECOVERY_STOP) \
    || defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP) \
    || defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_CLEANUP)
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

static int gate_recovery_root_stop(pid_t process_id, int number) {
    kill_function kill_next = next_kill();
    if (process_id > 0
        && number == SIGKILL
        && atomic_load(&failed_group_stop) != 0
        && getenv("MCP_CONSOLE_TEST_RECOVERY_ROOT_STOPPED") != NULL
        && atomic_exchange(&gated_recovery_root_stop, 1) == 0) {
        int result = kill_next(process_id, number);
        if (result == 0) {
            signal_checkpoint("MCP_CONSOLE_TEST_RECOVERY_ROOT_STOPPED");
            wait_for_release("MCP_CONSOLE_TEST_RECOVERY_ROOT_RELEASE");
        }
        return result;
    }
    return kill_next(process_id, number);
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_START) \
    || defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP) \
    || defined(MCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE) \
    || defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_CLEANUP)
static int is_subcommand(const char *name) {
    int argc = *_NSGetArgc();
    char **argv = *_NSGetArgv();
    return argc > 1 && strcmp(argv[1], name) == 0;
}
#endif

__attribute__((constructor))
static void configure_interposer(void) {
#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_START) \
    || defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP) \
    || defined(MCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE) \
    || defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_CLEANUP)
    if (!is_subcommand("sandbox-manager") && !is_subcommand("sandbox")) {
        unsetenv("DYLD_INSERT_LIBRARIES");
    }
#else
    unsetenv("DYLD_INSERT_LIBRARIES");
#endif
}

#if defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP) \
    || defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_CLEANUP)
static int gate_manager_group_cleanup(pid_t process_group_id, int number) {
    int result = next_killpg()(process_group_id, number);
    int saved_errno = errno;
    if (number == SIGKILL && is_subcommand("sandbox-manager")) {
        if (atomic_exchange(&gated_manager_group_cleanup, 1) != 0) {
            errno = EIO;
            return -1;
        }
#if defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP)
        if (getenv("MCP_CONSOLE_TEST_LATE_CLEANUP") != NULL) {
            signal_checkpoint("MCP_CONSOLE_TEST_LATE_CLEANUP");
            wait_for_release("MCP_CONSOLE_TEST_LATE_CLEANUP_RELEASE");
        }
#else
        if (getenv("MCP_CONSOLE_TEST_RETIREMENT_CLEANUP") != NULL) {
            signal_checkpoint("MCP_CONSOLE_TEST_RETIREMENT_CLEANUP");
            wait_for_release("MCP_CONSOLE_TEST_RETIREMENT_RELEASE");
        }
#endif
    }
    errno = saved_errno;
    return result;
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_START)
static ssize_t gate_manager_initialization(
    int descriptor,
    void *buffer,
    size_t length,
    int flags
) {
    if (descriptor == STDIN_FILENO
        && getenv("MCP_CONSOLE_TEST_MANAGER_START") != NULL
        && is_subcommand("sandbox-manager")
        && atomic_exchange(&gated_manager_read, 1) == 0) {
        signal_checkpoint("MCP_CONSOLE_TEST_MANAGER_START");
        wait_for_release("MCP_CONSOLE_TEST_MANAGER_RELEASE");
    }
    return recvfrom(descriptor, buffer, length, flags, NULL, NULL);
}

#endif

#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE)
static int observe_manager_process_watches(
    int descriptor,
    const struct kevent *changes,
    int change_count,
    struct kevent *events,
    int event_count,
    const struct timespec *timeout
) {
    if (is_subcommand("sandbox-manager")
        && changes == NULL
        && change_count == 0
        && events != NULL
        && event_count > 0
        && timeout == NULL
        && atomic_load(&manager_observed_descendant) != 0
        && atomic_exchange(&manager_descendant_observed_reported, 1) == 0) {
        signal_checkpoint("MCP_CONSOLE_TEST_MANAGER_DESCENDANT_OBSERVED");
    }

    int result = next_kevent()(
        descriptor,
        changes,
        change_count,
        events,
        event_count,
        timeout
    );
    if (result >= 0
        && is_subcommand("sandbox-manager")
        && changes != NULL
        && change_count == 1
        && changes[0].filter == EVFILT_PROC) {
        pid_t process_id = (pid_t)changes[0].ident;
        if ((changes[0].flags & EV_DELETE) != 0
            && process_id == atomic_load(&manager_observed_descendant)) {
            atomic_store(&manager_observed_descendant, 0);
        } else if ((changes[0].flags & EV_ADD) != 0
            && (changes[0].fflags & NOTE_EXIT) != 0) {
            pid_t root = atomic_load(&manager_observed_root);
            if (root == 0) {
                atomic_store(&manager_observed_root, process_id);
            } else if (process_id != root
                && atomic_load(&manager_observed_descendant) == 0) {
                atomic_store(&manager_observed_descendant, process_id);
            }
        }
    }
    return result;
}

static int fail_manager_group_stop(pid_t process_group_id, int number) {
    if (number == SIGKILL && is_subcommand("sandbox-manager")) {
        atomic_store(&manager_group_stop_started, 1);
        if (getenv("MCP_CONSOLE_TEST_MANAGER_GROUP_STOP_FAILURE") != NULL) {
            signal_checkpoint("MCP_CONSOLE_TEST_MANAGER_GROUP_STOP_FAILURE");
            errno = EPERM;
            return -1;
        }
    }
    return next_killpg()(process_group_id, number);
}

static int fail_manager_root_stop(pid_t process_id, int number) {
    if (process_id > 0
        && number == SIGKILL
        && is_subcommand("sandbox-manager")
        && atomic_load(&manager_group_stop_started) != 0) {
        pid_t root = atomic_load(&manager_observed_root);
        pid_t descendant = atomic_load(&manager_observed_descendant);
        if (descendant != 0 && process_id == descendant) {
            int result = next_kill()(process_id, number);
            if (result == 0
                && atomic_exchange(&manager_descendant_stop_reported, 1) == 0) {
                signal_checkpoint("MCP_CONSOLE_TEST_MANAGER_DESCENDANT_SIGNAL");
            }
            return result;
        }
        if (root != 0 && process_id == root) {
            if (atomic_exchange(&manager_root_stop_reported, 1) == 0) {
                signal_checkpoint("MCP_CONSOLE_TEST_MANAGER_ROOT_STOP_FAILURE");
            }
            errno = EPERM;
            return -1;
        }
    }
    kill_function kill_next = next_kill();
    if (kill_next == NULL) {
        errno = ENOSYS;
        return -1;
    }
    return kill_next(process_id, number);
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_DENIED_SIGKILL)
static int deny_first_sigkill(pid_t process_id, int number) {
    if (number == SIGKILL
        && getenv("MCP_CONSOLE_TEST_DENIED_SIGKILL") != NULL
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
static int deny_first_sigkill(pid_t process_id, int number) {
    if (number == SIGKILL
        && getenv("MCP_CONSOLE_TEST_DENIED_SIGKILL") != NULL
        && is_subcommand("sandbox")
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
#elif defined(MCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE)
DYLD_INTERPOSE(observe_manager_process_watches, kevent)
DYLD_INTERPOSE(fail_manager_group_stop, killpg)
DYLD_INTERPOSE(fail_manager_root_stop, kill)
#elif defined(MCP_CONSOLE_INTERPOSE_DENIED_SIGKILL)
DYLD_INTERPOSE(deny_first_sigkill, kill)
#elif defined(MCP_CONSOLE_INTERPOSE_FAILED_RECOVERY_STOP)
DYLD_INTERPOSE(fail_process_info, proc_pidinfo)
DYLD_INTERPOSE(fail_group_stop, killpg)
DYLD_INTERPOSE(gate_recovery_root_stop, kill)
#elif defined(MCP_CONSOLE_INTERPOSE_FAILED_ROOT_OBSERVER)
DYLD_INTERPOSE(arm_root_identity_recheck, kevent)
DYLD_INTERPOSE(fail_root_observer, proc_pidinfo)
DYLD_INTERPOSE(fail_root_group_stop, kill)
#elif defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP)
DYLD_INTERPOSE(gate_manager_group_cleanup, killpg)
DYLD_INTERPOSE(deny_first_sigkill, kill)
DYLD_INTERPOSE(gate_root_reap, waitpid)
DYLD_INTERPOSE(delay_late_recovery, proc_pidinfo)
#elif defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_CLEANUP)
DYLD_INTERPOSE(gate_manager_group_cleanup, killpg)
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


def _remaining_timeout(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


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


def _wait_for_gated_root_and_manager(
    launcher: DarwinProcessIdentity,
) -> tuple[DarwinProcessIdentity, DarwinProcessIdentity]:
    deadline = time.monotonic() + TIMEOUT
    while True:
        children = tuple(darwin_child_process_identities(launcher))
        assert len(children) <= 2, children
        gated = tuple(
            child
            for child in children
            if darwin_process_waits_for_startup_release(child)
        )
        assert len(gated) <= 1, (children, gated)
        if len(children) == 2 and gated:
            root = gated[0]
            manager = next(child for child in children if child != root)
            return root, manager
        assert live_darwin_processes((launcher,)) == [launcher[0]], launcher
        assert time.monotonic() < deadline, (
            "launcher did not expose its gated root and manager"
        )
        time.sleep(0.01)


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
    # manager's readiness byte has been received. This is a causal readiness
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
    move_root_to_descendant_group: bool = False,
) -> _SandboxLifetime:
    assert not (detached and move_root_to_descendant_group)
    # The detached child leaves the root's session, so cleanup must come from
    # exact descendant observation rather than an inherited process group.
    child_group_option = (
        "process_group=0"
        if move_root_to_descendant_group
        else f"start_new_session={detached!r}"
    )
    root_group_setup = (
        "\n        os.setpgid(0, child.pid)" if move_root_to_descendant_group else ""
    )
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
            {child_group_option},
        ){root_group_setup}
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
            "the sandbox root, descendant, and temporary directory",
        )
        temporary_directory = Path(temporary_directory_text)
        root = capture_darwin_process_identity(int(root_pid))
        identities.append(root)
        descendant = capture_darwin_process_identity(int(descendant_pid))
        identities.append(descendant)
        launcher = capture_darwin_process_identity(process.pid)
        if move_root_to_descendant_group:
            assert os.getpgid(root[0]) == descendant[0], (
                "sandbox root did not join its descendant's process group"
            )
        elif detached:
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
    identities = (lifetime.root, lifetime.descendant, lifetime.manager)
    kill_darwin_processes(identities)
    _wait_for_process_exit(identities, "sandbox cleanup did not stop all processes")
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


__all__ = [name for name in globals() if name not in {"__builtins__"}]
