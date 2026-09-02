#!/usr/bin/env -S uv run --script

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
    darwin_process_waits_for_control,
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
        "manager-thread-start-failure": (
            "-DMCP_CONSOLE_INTERPOSE_MANAGER_THREAD_START_FAILURE"
        ),
        "manager-stop-failure": "-DMCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE",
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
static _Atomic int manager_group_stop_reported = 0;
static _Atomic pid_t manager_direct_root = 0;
static _Atomic int manager_root_stop_reported = 0;
static _Atomic pid_t manager_observed_root = 0;
static _Atomic pid_t manager_observed_descendant = 0;
static _Atomic int manager_descendant_observed_reported = 0;
static _Atomic int manager_descendant_stop_reported = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP)
static _Atomic int delayed_cleanup = 0;
static _Atomic int delayed_late_recovery = 0;
static _Atomic int reaped_root = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_START)
static _Atomic int gated_manager_read = 0;
#endif
#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_THREAD_START_FAILURE)
static _Atomic int failed_manager_thread_start = 0;
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
#if defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_DISPOSITION)
static _Atomic int gated_retirement_disposition = 0;
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

#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE)
typedef int (*killpg_function)(pid_t, int);

static killpg_function next_killpg(void) {
    return killpg;
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_FAILED_RECOVERY_STOP) \
    || defined(MCP_CONSOLE_INTERPOSE_FAILED_ROOT_OBSERVER) \
    || defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP) \
    || defined(MCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE)
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

#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE)
static int mark_manager_direct_root_stop(
    int process_id,
    int flavor,
    uint64_t argument,
    void *buffer,
    int buffer_size
) {
    if (flavor == PROC_PIDTBSDINFO
        && pthread_main_np() != 0
        && atomic_load(&manager_group_stop_started) != 0) {
        atomic_store(&manager_direct_root, process_id);
    }
    return next_proc_pidinfo()(process_id, flavor, argument, buffer, buffer_size);
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
    || defined(MCP_CONSOLE_INTERPOSE_MANAGER_THREAD_START_FAILURE) \
    || defined(MCP_CONSOLE_INTERPOSE_FAILED_RECOVERY_STOP) \
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
    || defined(MCP_CONSOLE_INTERPOSE_MANAGER_THREAD_START_FAILURE) \
    || defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP) \
    || defined(MCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE) \
    || defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_DISPOSITION)
static int is_subcommand(const char *name) {
    int argc = *_NSGetArgc();
    char **argv = *_NSGetArgv();
    return argc > 1 && strcmp(argv[1], name) == 0;
}
#endif

__attribute__((constructor))
static void configure_interposer(void) {
#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_START) \
    || defined(MCP_CONSOLE_INTERPOSE_MANAGER_THREAD_START_FAILURE) \
    || defined(MCP_CONSOLE_INTERPOSE_LATE_CLEANUP) \
    || defined(MCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE) \
    || defined(MCP_CONSOLE_INTERPOSE_RETIREMENT_DISPOSITION)
    if (!is_subcommand("sandbox-manager") && !is_subcommand("sandbox")) {
        unsetenv("DYLD_INSERT_LIBRARIES");
    }
#else
    unsetenv("DYLD_INSERT_LIBRARIES");
#endif
}

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

#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_THREAD_START_FAILURE)
typedef int (*pthread_create_function)(
    pthread_t *,
    const pthread_attr_t *,
    void *(*)(void *),
    void *
);

static pthread_create_function next_pthread_create(void) {
    return pthread_create;
}

static int fail_manager_tracker_start(
    pthread_t *thread,
    const pthread_attr_t *attributes,
    void *(*start_routine)(void *),
    void *argument
) {
    if (is_subcommand("sandbox-manager")
        && getenv("MCP_CONSOLE_TEST_MANAGER_THREAD_START_FAILURE") != NULL
        && atomic_exchange(&failed_manager_thread_start, 1) == 0) {
        signal_checkpoint("MCP_CONSOLE_TEST_MANAGER_THREAD_START_FAILURE");
        wait_for_release("MCP_CONSOLE_TEST_MANAGER_THREAD_START_RELEASE");
        return EAGAIN;
    }
    return next_pthread_create()(thread, attributes, start_routine, argument);
}
#endif

#if defined(MCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE)
static int observe_manager_tracker(
    int descriptor,
    const struct kevent *changes,
    int change_count,
    struct kevent *events,
    int event_count,
    const struct timespec *timeout
) {
    if (is_subcommand("sandbox-manager")
        && pthread_main_np() == 0
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
        pid_t direct_root = atomic_load(&manager_direct_root);
        if (pthread_main_np() == 0
            && direct_root != 0
            && process_id != direct_root) {
            int result = next_kill()(process_id, number);
            if (result == 0
                && process_id == atomic_load(&manager_observed_descendant)
                && atomic_exchange(&manager_descendant_stop_reported, 1) == 0) {
                signal_checkpoint("MCP_CONSOLE_TEST_MANAGER_DESCENDANT_SIGNAL");
            }
            return result;
        }
        if (process_id == direct_root
            && atomic_exchange(&manager_root_stop_reported, 1) == 0) {
            signal_checkpoint("MCP_CONSOLE_TEST_MANAGER_ROOT_STOP_FAILURE");
        } else if (atomic_exchange(&manager_group_stop_reported, 1) == 0) {
            signal_checkpoint("MCP_CONSOLE_TEST_MANAGER_GROUP_STOP_FAILURE");
        }
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
static ssize_t delay_cleanup_acknowledgement(
    int descriptor,
    const void *buffer,
    size_t length,
    int flags
) {
    const unsigned char cleanup_complete = 5;
    const unsigned char preserve_temporary_directory = 4;
    if (descriptor == STDIN_FILENO
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
#elif defined(MCP_CONSOLE_INTERPOSE_MANAGER_THREAD_START_FAILURE)
DYLD_INTERPOSE(fail_manager_tracker_start, pthread_create)
#elif defined(MCP_CONSOLE_INTERPOSE_MANAGER_STOP_FAILURE)
DYLD_INTERPOSE(observe_manager_tracker, kevent)
DYLD_INTERPOSE(fail_manager_group_stop, killpg)
DYLD_INTERPOSE(fail_manager_root_stop, kill)
DYLD_INTERPOSE(mark_manager_direct_root_stop, proc_pidinfo)
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


def _wait_for_manager_disposition(lifetime: _SandboxLifetime) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        assert lifetime.temporary_directory.exists(), (
            "manager removed the temporary directory before launcher disposition"
        )
        if darwin_process_waits_for_control(lifetime.manager):
            return
        time.sleep(0.01)
    raise AssertionError("manager did not wait for launcher disposition")


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
            root, manager = _wait_for_gated_root_and_manager(launcher)
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


def test_terminal_interrupt_before_manager_readiness_preserves_status(
    binary: Path,
) -> Transcript:
    arguments = ("sandbox", "--", "python", "-c", "raise SystemExit(23)")

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

        process, master = _start_with_controlling_terminal(
            [binary, *arguments],
            environment,
        )
        identities: list[DarwinProcessIdentity] = []
        manager_released = False
        sandbox_temporary_directory: Path | None = None
        try:
            manager_started.wait("manager startup before readiness")
            launcher = capture_darwin_process_identity(process.pid)
            root, manager = _wait_for_gated_root_and_manager(launcher)
            identities.extend((root, manager))
            temporary_directories = list(
                fixture_directory.glob(f"mcp-console-tmp-{process.pid}-*")
            )
            assert len(temporary_directories) == 1, temporary_directories
            sandbox_temporary_directory = temporary_directories[0]

            assert os.getpgid(root[0]) == root[0]
            assert os.tcgetpgrp(master) == root[0]
            terminal_attributes = termios.tcgetattr(master)
            assert terminal_attributes[3] & termios.ISIG
            assert terminal_attributes[6][termios.VINTR] == b"\x03"
            exit_queue = select.kqueue()
            try:
                exit_watch = select.kevent(
                    root[0],
                    filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=select.KQ_NOTE_EXIT,
                )
                assert exit_queue.control([exit_watch], 0, 0) == []
                os.write(master, b"\x03")
                exit_events = exit_queue.control(None, 1, TIMEOUT)
                assert len(exit_events) == 1, "sandbox root did not exit"
                exit_event = exit_events[0]
                assert exit_event.ident == root[0], exit_event
                assert exit_event.filter == select.KQ_FILTER_PROC, exit_event
                assert exit_event.fflags & select.KQ_NOTE_EXIT, exit_event
            finally:
                exit_queue.close()

            manager_release.release()
            manager_released = True
            returncode = process.wait(timeout=TIMEOUT)
            stdout = process.stdout.read().decode("utf-8")
            stderr = process.stderr.read().decode("utf-8")
            survivors = live_darwin_processes(identities)

            assert returncode == 130, returncode
            assert stdout == "", stdout
            assert stderr == "", stderr
            assert survivors == [], f"sandbox processes survived: {survivors}"
            assert not sandbox_temporary_directory.exists(), (
                "sandbox temporary directory survived normal retirement"
            )
            return [
                {
                    "command": _command(*arguments),
                    "manager_checkpoint": "before readiness",
                    "terminal_foreground_group": "gated sandbox root",
                },
                {
                    "stdin": "<Ctrl-C>",
                    "root_state_before_manager_release": "exit observed",
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
            for stream in (process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()
            os.close(master)
            manager_started.close()
            manager_release.close()


def test_pending_signal_at_root_exit_preserves_status(binary: Path) -> Transcript:
    lifetime = _start_lifetime(binary)
    exit_events = select.kqueue()
    launcher_resumed = False
    try:
        root_exit = select.kevent(
            lifetime.root[0],
            filter=select.KQ_FILTER_PROC,
            flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
            fflags=select.KQ_NOTE_EXIT,
        )
        assert exit_events.control([root_exit], 0, 0) == []
        assert signal_darwin_process(lifetime.launcher, signal.SIGSTOP), (
            "sandbox launcher exited before stop injection"
        )
        _wait_for_process_state(lifetime.launcher, "T", "sandbox launcher")

        assert signal_darwin_process(lifetime.launcher, signal.SIGTERM), (
            "sandbox launcher exited before pending-signal injection"
        )
        lifetime.process.stdin.write(b"exit\n")
        lifetime.process.stdin.close()
        events = exit_events.control(None, 1, TIMEOUT)
        assert len(events) == 1, "sandbox root did not exit while launcher was stopped"
        assert events[0].ident == lifetime.root[0], events[0]
        assert events[0].filter == select.KQ_FILTER_PROC, events[0]
        assert events[0].fflags & select.KQ_NOTE_EXIT, events[0]

        assert signal_darwin_process(lifetime.launcher, signal.SIGCONT), (
            "sandbox launcher exited before resume injection"
        )
        launcher_resumed = True
        returncode = lifetime.process.wait(timeout=TIMEOUT)
        stderr = lifetime.process.stderr.read().decode("utf-8")
        survivors = _wait_for_cleanup(lifetime)

        assert returncode == 23, (returncode, stderr)
        assert stderr == "", stderr
        assert survivors == [], f"sandbox processes survived root exit: {survivors}"
        assert not lifetime.temporary_directory.exists(), (
            "pending launcher signal preserved the sandbox temporary directory"
        )
        return [
            _command_record(lifetime),
            {
                "launcher_signal": "SIGSTOP",
                "pending_launcher_signal": "SIGTERM",
                "root_action": "exit 23",
                "verified_pending_signal": "before launcher resume",
            },
            {
                "launcher_signal": "SIGCONT",
                "launcher_returncode": returncode,
                "verified_signal": "consumed without replacing root status",
                "verified_cleanup": (
                    "sandbox root, detached descendant, manager, and temp"
                ),
            },
        ]
    finally:
        if not launcher_resumed:
            signal_darwin_process(lifetime.launcher, signal.SIGCONT)
        exit_events.close()
        _cleanup(lifetime)


def test_pending_signal_during_failed_commit_preserves_error(
    binary: Path,
) -> Transcript:
    arguments = ("sandbox", "--", "python", "-c", "raise SystemExit(23)")

    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        committed_ready = FifoCheckpoint(temporary / "committed-ready")
        committed_release = FifoCheckpoint(temporary / "committed-release")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_MANAGER_COMMITTED_READY"] = str(
            committed_ready.path
        )
        environment["MCP_CONSOLE_TEST_MANAGER_COMMITTED_RELEASE"] = str(
            committed_release.path
        )
        environment["DYLD_INSERT_LIBRARIES"] = str(build_manager_interposer(temporary))
        process = subprocess.Popen(
            [binary, *arguments],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        identities: tuple[DarwinProcessIdentity, ...] = ()
        sandbox_temporary_directory: Path | None = None
        try:
            committed_ready.wait("manager COMMITTED write")
            launcher = capture_darwin_process_identity(process.pid)
            root, manager = _wait_for_gated_root_and_manager(launcher)
            identities = (root, manager)
            sandbox_directories = tuple(
                temporary.glob(f"mcp-console-tmp-{process.pid}-*")
            )
            assert len(sandbox_directories) == 1, sandbox_directories
            sandbox_temporary_directory = sandbox_directories[0]

            assert signal_darwin_process(launcher, signal.SIGTERM), (
                "sandbox launcher exited before pending-signal injection"
            )
            assert signal_darwin_process(manager, signal.SIGKILL), (
                "manager exited before commit-failure injection"
            )
            returncode = process.wait(timeout=TIMEOUT)
            stdout = process.stdout.read().decode("utf-8")
            stderr = process.stderr.read().decode("utf-8")
            survivors = live_darwin_processes(identities)

            assert returncode == 1, (returncode, stderr)
            assert stdout == "", stdout
            assert survivors == [], f"sandbox processes survived: {survivors}"
            assert sandbox_temporary_directory.exists(), (
                "ambiguous manager commit removed sandbox temporary directory"
            )
            return [
                {
                    "command": _command(*arguments),
                    "manager_checkpoint": "before COMMITTED",
                    "pending_launcher_signal": "SIGTERM",
                    "manager_signal": "SIGKILL",
                },
                {
                    "launcher_returncode": returncode,
                    "stderr": stderr,
                    "verified_signal": "consumed without replacing startup error",
                    "verified_cleanup": "gated sandbox root and manager",
                    "verified_preservation": "sandbox temp",
                },
            ]
        finally:
            committed_release.release()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=TIMEOUT)
            kill_darwin_processes(identities)
            if sandbox_temporary_directory is not None:
                shutil.rmtree(sandbox_temporary_directory, ignore_errors=True)
            for stream in (process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()
            committed_ready.close()
            committed_release.close()


def test_manager_panic_during_commit_preserves_temporary_directory(
    binary: Path,
) -> Transcript:
    arguments = ("sandbox", "--", "python", "-c", "raise SystemExit(23)")

    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_directory = Path(temporary_directory)
        thread_start_failed = FifoCheckpoint(
            fixture_directory / "manager-thread-start-failed"
        )
        thread_start_release = FifoCheckpoint(
            fixture_directory / "manager-thread-start-release"
        )
        environment = os.environ.copy()
        environment.update(
            {
                "DYLD_INSERT_LIBRARIES": str(
                    _build_supervision_interposer(
                        fixture_directory,
                        "manager-thread-start-failure",
                    )
                ),
                "MCP_CONSOLE_TEST_MANAGER_THREAD_START_FAILURE": str(
                    thread_start_failed.path
                ),
                "MCP_CONSOLE_TEST_MANAGER_THREAD_START_RELEASE": str(
                    thread_start_release.path
                ),
                "RUST_BACKTRACE": "0",
                "TMPDIR": str(fixture_directory),
            }
        )
        process = subprocess.Popen(
            [binary, *arguments],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        identities: list[DarwinProcessIdentity] = []
        thread_start_released = False
        sandbox_temporary_directory: Path | None = None
        try:
            thread_start_failed.wait("manager tracker-thread start failure")
            launcher = capture_darwin_process_identity(process.pid)
            root, manager = _wait_for_gated_root_and_manager(launcher)
            identities.extend((root, manager))
            temporary_directories = list(
                fixture_directory.glob(f"mcp-console-tmp-{process.pid}-*")
            )
            assert len(temporary_directories) == 1, temporary_directories
            sandbox_temporary_directory = temporary_directories[0]

            thread_start_release.release()
            thread_start_released = True
            returncode = process.wait(timeout=TIMEOUT)
            stdout = process.stdout.read().decode("utf-8")
            stderr = process.stderr.read().decode("utf-8")
            survivors = live_darwin_processes(identities)

            assert returncode == 1, (returncode, stderr)
            assert stdout == "", stdout
            assert stderr == (
                "sandbox manager did not confirm ownership: failed to fill whole buffer\n"
            ), stderr
            assert survivors == [], (
                f"manager panic leaked sandbox processes: {survivors}"
            )
            assert sandbox_temporary_directory.exists(), (
                "manager panic removed the sandbox temporary directory"
            )
            return [
                {
                    "command": _command(*arguments),
                    "manager_checkpoint": "before tracker-thread creation",
                    "manager_thread_start": "EAGAIN",
                },
                {
                    "launcher_returncode": returncode,
                    "stdout": stdout,
                    "stderr": stderr,
                    "verified_cleanup": "gated sandbox root and manager",
                    "verified_preservation": "sandbox temp",
                },
            ]
        finally:
            if not thread_start_released:
                thread_start_release.release()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=TIMEOUT)
            kill_darwin_processes(identities)
            if sandbox_temporary_directory is not None:
                shutil.rmtree(sandbox_temporary_directory, ignore_errors=True)
            for stream in (process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()
            thread_start_failed.close()
            thread_start_release.close()


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


def test_manager_owner_loss_stop_failure_remains_bounded(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_directory = Path(temporary_directory)
        group_stop_failed = FifoCheckpoint(
            fixture_directory / "manager-group-stop-failed"
        )
        root_stop_failed = FifoCheckpoint(
            fixture_directory / "manager-root-stop-failed"
        )
        descendant_observed = FifoCheckpoint(
            fixture_directory / "manager-descendant-observed"
        )
        descendant_signaled = FifoCheckpoint(
            fixture_directory / "manager-descendant-signaled"
        )
        environment = os.environ.copy()
        environment.update(
            {
                "DYLD_INSERT_LIBRARIES": str(
                    _build_supervision_interposer(
                        fixture_directory,
                        "manager-stop-failure",
                    )
                ),
                "MCP_CONSOLE_TEST_MANAGER_GROUP_STOP_FAILURE": str(
                    group_stop_failed.path
                ),
                "MCP_CONSOLE_TEST_MANAGER_ROOT_STOP_FAILURE": str(
                    root_stop_failed.path
                ),
                "MCP_CONSOLE_TEST_MANAGER_DESCENDANT_OBSERVED": str(
                    descendant_observed.path
                ),
                "MCP_CONSOLE_TEST_MANAGER_DESCENDANT_SIGNAL": str(
                    descendant_signaled.path
                ),
            }
        )
        lifetime = _start_lifetime(binary, environment)
        try:
            descendant_observed.wait("manager observation of detached descendant")
            with (
                _observe_process_exit(lifetime.manager) as manager_exit,
                _observe_process_exit(lifetime.descendant) as descendant_exit,
            ):
                deadline = time.monotonic() + TIMEOUT
                assert signal_darwin_process(lifetime.launcher, signal.SIGKILL), (
                    "sandbox launcher exited before owner-loss injection"
                )
                returncode = lifetime.process.wait(timeout=_remaining_timeout(deadline))
                group_stop_failed.wait(
                    "manager process-group stop failure",
                    _remaining_timeout(deadline),
                )
                root_stop_failed.wait(
                    "manager direct-root stop failure",
                    _remaining_timeout(deadline),
                )
                descendant_signaled.wait(
                    "manager tracker descendant signal",
                    _remaining_timeout(deadline),
                )
                descendant_events = descendant_exit.control(
                    None,
                    1,
                    _remaining_timeout(deadline),
                )
                events = manager_exit.control(
                    None,
                    1,
                    _remaining_timeout(deadline),
                )

            assert descendant_events, (
                "sandbox tracker did not retire the observed detached descendant"
            )
            assert descendant_events[0].ident == lifetime.descendant[0], (
                descendant_events[0]
            )
            assert descendant_events[0].filter == select.KQ_FILTER_PROC, (
                descendant_events[0]
            )
            assert descendant_events[0].fflags & select.KQ_NOTE_EXIT, descendant_events[
                0
            ]
            assert events, (
                "sandbox manager did not exit after its bounded cleanup interval"
            )
            assert events[0].ident == lifetime.manager[0], events[0]
            assert events[0].filter == select.KQ_FILTER_PROC, events[0]
            assert events[0].fflags & select.KQ_NOTE_EXIT, events[0]
            assert returncode == -signal.SIGKILL, returncode
            _wait_for_process_exit(
                (lifetime.manager,),
                "sandbox manager remained live after bounded cleanup",
            )
            _wait_for_process_state(
                lifetime.descendant,
                "Z",
                "retired sandbox descendant",
            )
            assert live_darwin_processes((lifetime.root,)) == [lifetime.root[0]], (
                "failed root termination unexpectedly stopped the sandbox root"
            )
            assert lifetime.temporary_directory.exists(), (
                "manager stop failure removed the sandbox temporary directory"
            )
            return [
                _command_record(lifetime),
                {
                    "launcher_signal": "SIGKILL",
                    "manager_group_stop_signal": "EPERM",
                    "manager_root_stop_signal": "EPERM",
                    "verified_bounded_return": "within the cleanup deadline",
                    "verified_cleanup": "observed detached descendant and manager",
                    "verified_preservation": "sandbox root and sandbox temp",
                },
            ]
        finally:
            group_stop_failed.close()
            root_stop_failed.close()
            descendant_observed.close()
            descendant_signaled.close()
            _cleanup(lifetime)


def test_manager_owner_loss_after_root_group_change_remains_bounded(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_directory = Path(temporary_directory)
        root_stop_failed = FifoCheckpoint(
            fixture_directory / "manager-root-stop-failed"
        )
        descendant_observed = FifoCheckpoint(
            fixture_directory / "manager-descendant-observed"
        )
        descendant_signaled = FifoCheckpoint(
            fixture_directory / "manager-descendant-signaled"
        )
        environment = os.environ.copy()
        environment.update(
            {
                "DYLD_INSERT_LIBRARIES": str(
                    _build_supervision_interposer(
                        fixture_directory,
                        "manager-stop-failure",
                    )
                ),
                "MCP_CONSOLE_TEST_MANAGER_ROOT_STOP_FAILURE": str(
                    root_stop_failed.path
                ),
                "MCP_CONSOLE_TEST_MANAGER_DESCENDANT_OBSERVED": str(
                    descendant_observed.path
                ),
                "MCP_CONSOLE_TEST_MANAGER_DESCENDANT_SIGNAL": str(
                    descendant_signaled.path
                ),
            }
        )
        lifetime = _start_lifetime(
            binary,
            environment,
            detached=False,
            move_root_to_descendant_group=True,
        )
        try:
            descendant_observed.wait("manager observation of new-group descendant")
            with (
                _observe_process_exit(lifetime.manager) as manager_exit,
                _observe_process_exit(lifetime.descendant) as descendant_exit,
            ):
                deadline = time.monotonic() + TIMEOUT
                assert signal_darwin_process(lifetime.launcher, signal.SIGKILL), (
                    "sandbox launcher exited before owner-loss injection"
                )
                returncode = lifetime.process.wait(timeout=_remaining_timeout(deadline))
                root_stop_failed.wait(
                    "manager direct-root stop failure",
                    _remaining_timeout(deadline),
                )
                descendant_signaled.wait(
                    "manager tracker descendant signal",
                    _remaining_timeout(deadline),
                )
                descendant_events = descendant_exit.control(
                    None,
                    1,
                    _remaining_timeout(deadline),
                )
                events = manager_exit.control(
                    None,
                    1,
                    _remaining_timeout(deadline),
                )

            assert descendant_events, (
                "sandbox tracker did not retire the observed new-group descendant"
            )
            assert descendant_events[0].ident == lifetime.descendant[0], (
                descendant_events[0]
            )
            assert descendant_events[0].filter == select.KQ_FILTER_PROC, (
                descendant_events[0]
            )
            assert descendant_events[0].fflags & select.KQ_NOTE_EXIT, descendant_events[
                0
            ]
            assert events, (
                "sandbox manager did not exit after its bounded cleanup interval"
            )
            assert events[0].ident == lifetime.manager[0], events[0]
            assert events[0].filter == select.KQ_FILTER_PROC, events[0]
            assert events[0].fflags & select.KQ_NOTE_EXIT, events[0]
            assert returncode == -signal.SIGKILL, returncode
            _wait_for_process_exit(
                (lifetime.manager,),
                "sandbox manager remained live after bounded cleanup",
            )
            _wait_for_process_state(
                lifetime.descendant,
                "Z",
                "retired sandbox descendant",
            )
            assert live_darwin_processes((lifetime.root,)) == [lifetime.root[0]], (
                "failed root termination unexpectedly stopped the sandbox root"
            )
            assert lifetime.temporary_directory.exists(), (
                "manager stop failure removed the sandbox temporary directory"
            )
            command = _command_record(lifetime)
            command["stdout"] = (
                "<sandbox root pid>\n<new-group descendant pid>\n<sandbox temp>\n"
            )
            return [
                command,
                {
                    "root_process_group": "descendant PID",
                    "launcher_signal": "SIGKILL",
                    "manager_old_group_stop": "already empty",
                    "manager_root_stop_signal": "EPERM",
                    "verified_bounded_return": "within the cleanup deadline",
                    "verified_cleanup": "observed new-group descendant and manager",
                    "verified_preservation": "sandbox root and sandbox temp",
                },
            ]
        finally:
            root_stop_failed.close()
            descendant_observed.close()
            descendant_signaled.close()
            _cleanup(lifetime)


def test_manager_crash_retires_the_sandbox_lifetime(binary: Path) -> Transcript:
    lifetime = _start_lifetime(binary)
    try:
        assert signal_darwin_process(lifetime.manager, signal.SIGKILL), (
            "manager exited before crash injection"
        )
        returncode = lifetime.process.wait(timeout=TIMEOUT)
        stderr = lifetime.process.stderr.read().decode("utf-8")
        _wait_for_process_exit(
            (lifetime.root, lifetime.descendant, lifetime.manager),
            "manager crash leaked sandbox processes",
        )

        assert returncode == 128 + signal.SIGKILL, returncode
        assert stderr == "", stderr
        assert lifetime.temporary_directory.exists(), (
            "manager recovery removed the sandbox temporary directory"
        )
        return [
            _command_record(lifetime),
            {
                "manager_signal": "SIGKILL",
                "launcher_returncode": returncode,
                "verified_cleanup": "sandbox root, detached descendant, and manager",
                "verified_preservation": "sandbox temp",
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
        root_stopped = FifoCheckpoint(fixture_directory / "root-stopped")
        root_stop_release = FifoCheckpoint(fixture_directory / "root-stop-release")
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
                "MCP_CONSOLE_TEST_RECOVERY_ROOT_STOPPED": str(root_stopped.path),
                "MCP_CONSOLE_TEST_RECOVERY_ROOT_RELEASE": str(root_stop_release.path),
            }
        )
        lifetime = _start_lifetime(binary, environment)
        root_stop_released = False
        try:
            with _observe_process_exit(lifetime.root) as root_exit:
                failure_trigger.touch()
                assert signal_darwin_process(lifetime.manager, signal.SIGKILL), (
                    "manager exited before crash injection"
                )
                inspection_failed.wait("launcher root-inspection failure")
                group_stop_failed.wait("launcher pinned-group stop failure")
                root_stopped.wait("launcher direct pinned-root termination")
                events = root_exit.control(None, 1, TIMEOUT)
                assert events, "direct pinned-root termination did not stop the root"
                assert events[0].ident == lifetime.root[0], events[0]
                assert events[0].filter == select.KQ_FILTER_PROC, events[0]
                assert events[0].fflags & select.KQ_NOTE_EXIT, events[0]
                root_stop_release.release()
                root_stop_released = True
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
            if not root_stop_released:
                root_stop_release.release()
            inspection_failed.close()
            group_stop_failed.close()
            root_stopped.close()
            root_stop_release.close()
            _cleanup(lifetime)


def test_root_observer_failure_reports_group_cleanup_failure(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        inspection_failed = FifoCheckpoint(temporary / "inspection-failed")
        group_stop_failed = FifoCheckpoint(temporary / "group-stop-failed")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
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


def test_cleanup_signal_after_root_exit_terminates_launcher(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_directory = Path(temporary_directory)
        late_cleanup = FifoCheckpoint(fixture_directory / "late-cleanup")
        late_cleanup_release = FifoCheckpoint(
            fixture_directory / "late-cleanup-release"
        )
        environment = os.environ.copy()
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_supervision_interposer(fixture_directory, "late-cleanup")
        )
        environment["MCP_CONSOLE_TEST_LATE_CLEANUP"] = str(late_cleanup.path)
        environment["MCP_CONSOLE_TEST_LATE_CLEANUP_RELEASE"] = str(
            late_cleanup_release.path
        )
        lifetime = _start_lifetime(binary, environment)
        cleanup_released = False
        try:
            lifetime.process.stdin.write(b"exit\n")
            lifetime.process.stdin.close()
            late_cleanup.wait("manager cleanup after sandbox root exit")
            assert signal_darwin_process(lifetime.launcher, signal.SIGTERM), (
                "launcher exited before cleanup signal"
            )
            returncode = lifetime.process.wait(timeout=TIMEOUT)
            stderr = lifetime.process.stderr.read().decode("utf-8")
            late_cleanup_release.release()
            cleanup_released = True
            _wait_for_process_exit(
                (lifetime.root, lifetime.descendant, lifetime.manager),
                "sandbox processes survived cleanup-time launcher signal",
            )

            assert returncode == -signal.SIGTERM, returncode
            assert stderr == "", stderr
            assert lifetime.temporary_directory.exists(), (
                "cleanup-time signal removed the sandbox temporary directory"
            )
            return [
                _command_record(lifetime),
                {
                    "manager_cleanup": "acknowledgement held after root exit",
                    "launcher_signal": "SIGTERM",
                    "launcher_returncode": returncode,
                    "verified_cleanup": "sandbox root, detached descendant, and manager",
                    "verified_preservation": "sandbox temp",
                },
            ]
        finally:
            if not cleanup_released:
                late_cleanup_release.release()
            late_cleanup.close()
            late_cleanup_release.close()
            _cleanup(lifetime)


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
