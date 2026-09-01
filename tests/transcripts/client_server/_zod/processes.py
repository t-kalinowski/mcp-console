import os
import signal
import subprocess
import time
from pathlib import Path

from _support import McpClient

from .coordination import FIXTURE_CHECKPOINT_TIMEOUT_SECONDS, wait_for_marker


def build_killpg_denial_interposer(directory: Path) -> Path:
    source = directory / "deny-killpg.c"
    library = directory / "deny-killpg.dylib"
    source.write_text(
        r"""
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <sys/types.h>
#include <unistd.h>

static pid_t denied_process_group = 0;
static int added_late_member = 0;
static pid_t seed_member = 0;
static pid_t late_member = 0;
static int killpg_count = 0;

static pid_t add_process_group_member(pid_t process_group);

static void signal_checkpoint(const char *name) {
    const char *checkpoint = getenv(name);
    if (checkpoint == NULL) {
        return;
    }
    int descriptor = open(checkpoint, O_WRONLY | O_NONBLOCK);
    if (descriptor >= 0) {
        const char signal = '1';
        syscall(SYS_write, descriptor, &signal, sizeof(signal));
        close(descriptor);
    }
}

static void write_pid_marker(const char *name, pid_t process_id) {
    const char *marker = getenv(name);
    if (marker == NULL) {
        return;
    }
    int descriptor = open(marker, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (descriptor >= 0) {
        dprintf(descriptor, "%d\n", process_id);
        close(descriptor);
    }
}

static void write_member_marker(pid_t process_id, pid_t process_group) {
    const char *marker = getenv("MCP_CONSOLE_TEST_LATE_MEMBER_MARKER");
    if (marker == NULL) {
        return;
    }
    int descriptor = open(marker, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (descriptor >= 0) {
        dprintf(descriptor, "%d %d\n", process_id, process_group);
        close(descriptor);
    }
}

static int deny_killpg(pid_t process_group, int signal) {
    if (signal == SIGKILL
        && getenv("MCP_CONSOLE_TEST_KILLPG_COUNT_MARKER") != NULL) {
        const char *marker = getenv("MCP_CONSOLE_TEST_KILLPG_COUNT_MARKER");
        int descriptor = open(marker, O_WRONLY | O_CREAT | O_TRUNC, 0600);
        if (descriptor >= 0) {
            killpg_count += 1;
            dprintf(descriptor, "%d %d\n", killpg_count, process_group);
            close(descriptor);
        }
    }
    if (signal == SIGKILL
        && getenv("MCP_CONSOLE_TEST_KILLPG_MARKER") != NULL) {
        denied_process_group = process_group;
        // Observed-tree retirement may leave no live relay descendant for the
        // fallback's first exact-group snapshot. Add one server child now; its
        // membership check adds the late child after that snapshot.
        seed_member = add_process_group_member(process_group);
        if (seed_member < 0) {
            return -1;
        }
        write_pid_marker("MCP_CONSOLE_TEST_KILLPG_MARKER", process_group);
        signal_checkpoint("MCP_CONSOLE_TEST_FORCE_STOP_REACHED");
        errno = EPERM;
        return -1;
    }
    if (signal == SIGINT
        && getenv("MCP_CONSOLE_TEST_DENIED_SIGINT") != NULL) {
        write_pid_marker("MCP_CONSOLE_TEST_DENIED_SIGINT", process_group);
        errno = EPERM;
        return -1;
    }
    return (int)syscall(SYS_kill, -process_group, signal);
}

static pid_t add_process_group_member(pid_t process_group) {
    int descriptors[2];
    if (pipe(descriptors) != 0) {
        return -1;
    }

    pid_t member = fork();
    if (member < 0) {
        close(descriptors[0]);
        close(descriptors[1]);
        return -1;
    }
    if (member == 0) {
        close(descriptors[0]);
        if (setpgid(0, process_group) != 0) {
            _exit(1);
        }
        pid_t process_id = getpid();
        if (write(descriptors[1], &process_id, sizeof(process_id))
            != sizeof(process_id)) {
            _exit(1);
        }
        close(descriptors[1]);
        for (;;) {
            pause();
        }
    }

    close(descriptors[1]);
    pid_t acknowledged_member = 0;
    ssize_t bytes_read;
    do {
        bytes_read = read(
            descriptors[0],
            &acknowledged_member,
            sizeof(acknowledged_member)
        );
    } while (bytes_read < 0 && errno == EINTR);
    int read_error = bytes_read < 0 ? errno : EIO;
    close(descriptors[0]);

    if (bytes_read != sizeof(acknowledged_member)
        || acknowledged_member != member) {
        syscall(SYS_kill, member, SIGKILL);
        while (waitpid(member, NULL, 0) < 0 && errno == EINTR) {
        }
        errno = read_error;
        return -1;
    }
    return member;
}

static pid_t getpgid_and_add_member(pid_t process_id) {
    pid_t process_group = (pid_t)syscall(SYS_getpgid, process_id);
    // Rust rechecks group membership only after taking its kernel snapshot.
    // Join the group here so a one-pass fallback cannot observe this child.
    if (process_group == denied_process_group && !added_late_member) {
        added_late_member = 1;
        pid_t member = add_process_group_member(process_group);
        if (member < 0) {
            return -1;
        }
        late_member = member;
        write_member_marker(member, process_group);
    }
    return process_group;
}

static int kill_and_reap_added_member(pid_t process_id, int signal) {
    int result = (int)syscall(SYS_kill, process_id, signal);
    int signal_error = errno;
    if (result == 0 && signal == SIGKILL
        && (process_id == seed_member || process_id == late_member)) {
        // Keep the final assertion independent of launchd's orphan reaping.
        int status = 0;
        pid_t waited;
        do {
            waited = waitpid(process_id, &status, 0);
        } while (waited < 0 && errno == EINTR);
        if (waited != process_id) {
            return -1;
        }
        if (process_id == late_member) {
            write_pid_marker("MCP_CONSOLE_TEST_LATE_MEMBER_REAP_MARKER", process_id);
            late_member = 0;
        } else {
            seed_member = 0;
        }
    }
    errno = signal_error;
    return result;
}

__attribute__((constructor))
static void remove_interposer_from_child_environment(void) {
    unsetenv("DYLD_INSERT_LIBRARIES");
}

__attribute__((used))
static struct {
    const void *replacement;
    const void *replacee;
} interposers[] __attribute__((section("__DATA,__interpose"))) = {
    {(const void *)&deny_killpg, (const void *)&killpg},
    {(const void *)&getpgid_and_add_member, (const void *)&getpgid},
    {(const void *)&kill_and_reap_added_member, (const void *)&kill},
};
""".removeprefix("\n"),
        encoding="utf-8",
    )
    subprocess.run(
        ["cc", "-dynamiclib", "-o", library, source],
        check=True,
        capture_output=True,
        text=True,
    )
    return library


def wait_for_stopped_worker(
    root: Path,
    previous_process_ids: set[int],
    recorded_workers: list[tuple[int, int]],
    client: McpClient,
) -> tuple[Path, int, int]:
    deadline = time.monotonic() + FIXTURE_CHECKPOINT_TIMEOUT_SECONDS
    while True:
        for marker in root.glob("mcp-console-tmp-*/zod-stop-continue-worker"):
            process_id, parent_id, process_group = map(
                int,
                marker.read_text(encoding="utf-8").split(),
            )
            if process_id in previous_process_ids:
                continue
            worker = (process_id, process_group)
            if worker not in recorded_workers:
                recorded_workers.append(worker)
            assert parent_id == process_group, (
                "stopped worker is not the relay's direct child"
            )
            assert process_id != process_group, (
                "stopped worker unexpectedly leads the relay process group"
            )
            assert process_group != os.getpgrp(), (
                "stopped worker shares the test process group"
            )
            status = read_process_status(process_id)
            if status is not None and status[2].startswith("T"):
                assert status[:2] == (parent_id, process_group), (
                    "stopped worker changed its process boundary"
                )
                return marker, process_id, process_group
        assert client.process.poll() is None, (
            "mcp-console stopped before its direct worker reached SIGSTOP"
        )
        assert time.monotonic() < deadline, (
            "direct worker did not enter the stopped process state within "
            f"{FIXTURE_CHECKPOINT_TIMEOUT_SECONDS} seconds"
        )
        time.sleep(0.01)


def wait_for_stopped_process(
    process_id: int,
    process_group: int,
    client: McpClient,
    description: str,
) -> None:
    deadline = time.monotonic() + FIXTURE_CHECKPOINT_TIMEOUT_SECONDS
    while True:
        status = read_process_status(process_id)
        assert status is not None, (
            f"{description} process exited before reaching SIGSTOP"
        )
        assert status[1] == process_group, f"{description} process changed groups"
        if status[2].startswith("T"):
            return
        assert client.process.poll() is None, (
            f"mcp-console stopped before {description} reached SIGSTOP"
        )
        assert time.monotonic() < deadline, (
            f"{description} did not reach SIGSTOP within "
            f"{FIXTURE_CHECKPOINT_TIMEOUT_SECONDS} seconds"
        )
        time.sleep(0.01)


def wait_for_path(path: Path, description: str, client: McpClient) -> None:
    observed = wait_for_marker(path.parent, path.name, client)
    assert observed == path, f"found a different path while waiting for {description}"


def read_process_status(process_id: int) -> tuple[int, int, str] | None:
    status = subprocess.run(
        [
            "ps",
            "-o",
            "ppid=",
            "-o",
            "pgid=",
            "-o",
            "state=",
            "-p",
            str(process_id),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if status.returncode == 1 and not status.stdout.strip():
        return None
    assert status.returncode == 0, status.stderr
    fields = status.stdout.split()
    assert len(fields) == 3, status.stdout
    return int(fields[0]), int(fields[1]), fields[2]


def continue_stopped_worker(process_id: int, process_group: int) -> None:
    status = read_process_status(process_id)
    assert status is not None, "stopped worker exited before SIGCONT"
    assert status[1] == process_group, "stopped worker changed process groups"
    assert status[2].startswith("T"), "worker was not stopped before SIGCONT"
    os.kill(process_id, signal.SIGCONT)


def wait_for_worker_retirement(
    process_id: int,
    process_group: int,
    client: McpClient,
) -> None:
    deadline = time.monotonic() + FIXTURE_CHECKPOINT_TIMEOUT_SECONDS
    while read_process_status(process_id) is not None or process_group_exists(
        process_group
    ):
        assert client.process.poll() is None, (
            "mcp-console stopped while retiring the old worker generation"
        )
        assert time.monotonic() < deadline, (
            "restart did not retire the old worker and relay process group"
        )
        time.sleep(0.01)


def stop_recorded_worker(process_id: int, process_group: int) -> None:
    assert process_group != os.getpgrp(), "refusing to stop the test process group"
    stop_process_group(process_group)
    status = read_process_status(process_id)
    if status is not None and status[1] == process_group:
        stop_process_id(process_id)


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


def process_exists(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_process_id(process_id: int | None) -> None:
    if process_id is None:
        return
    try:
        os.kill(process_id, signal.SIGKILL)
    except ProcessLookupError:
        pass


def wait_for_process_group_exit(process_group: int, client: McpClient) -> None:
    deadline = time.monotonic() + FIXTURE_CHECKPOINT_TIMEOUT_SECONDS
    while process_group_exists(process_group):
        assert client.process.poll() is None, "mcp-console stopped during restart"
        assert time.monotonic() < deadline, (
            "restart did not enforce its shutdown deadline"
        )
        time.sleep(0.01)


def stop_process_group(process_group: int | None) -> None:
    if process_group is None:
        return
    assert process_group > 0, process_group
    assert process_group != os.getpgrp(), process_group
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait()
