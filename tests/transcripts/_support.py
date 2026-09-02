import base64
import ctypes
import errno
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from typing import Any, Sequence

TranscriptEntry = dict[str, Any]
Transcript = list[TranscriptEntry]
ToolResult = dict[str, Any]
YamlStream = list[Any]
DarwinProcessIdentity = tuple[int, int, int]


class _DarwinProcessInfo(ctypes.Structure):
    # In Darwin's stable proc_bsdinfo ABI, the two start-time fields follow a
    # 120-byte prefix and complete the 136-byte structure.
    _fields_ = [
        ("prefix", ctypes.c_byte * 120),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


class _DarwinProcessFdInfo(ctypes.Structure):
    _fields_ = [
        ("fd", ctypes.c_int32),
        ("fdtype", ctypes.c_uint32),
    ]


class _DarwinThreadInfo(ctypes.Structure):
    _fields_ = [
        ("user_time", ctypes.c_uint64),
        ("system_time", ctypes.c_uint64),
        ("cpu_usage", ctypes.c_int32),
        ("policy", ctypes.c_int32),
        ("run_state", ctypes.c_int32),
        ("flags", ctypes.c_int32),
        ("sleep_time", ctypes.c_int32),
        ("current_priority", ctypes.c_int32),
        ("priority", ctypes.c_int32),
        ("max_priority", ctypes.c_int32),
        ("name", ctypes.c_char * 64),
    ]


_LIBPROC = None
if sys.platform == "darwin":
    _LIBPROC = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    _LIBPROC.proc_listchildpids.argtypes = [
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    _LIBPROC.proc_listchildpids.restype = ctypes.c_int
    _LIBPROC.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    _LIBPROC.proc_pidinfo.restype = ctypes.c_int


def current_darwin_process_identity(pid: int) -> DarwinProcessIdentity | None:
    assert _LIBPROC is not None
    proc_pidtbsdinfo = 3
    include_zombies = 1
    info = _DarwinProcessInfo()
    ctypes.set_errno(0)
    size = _LIBPROC.proc_pidinfo(
        pid,
        proc_pidtbsdinfo,
        include_zombies,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if size == ctypes.sizeof(info):
        return (pid, info.pbi_start_tvsec, info.pbi_start_tvusec)
    error = ctypes.get_errno()
    if size == 0 and error == errno.ESRCH:
        return None
    if size == 0 and error != 0:
        raise OSError(error, f"failed to inspect process {pid}")
    raise RuntimeError(
        f"proc_pidinfo returned {size} bytes for process {pid}, "
        f"expected {ctypes.sizeof(info)}"
    )


def capture_darwin_process_identity(pid: int) -> DarwinProcessIdentity:
    identity = current_darwin_process_identity(pid)
    assert identity is not None, f"process {pid} exited before identity capture"
    return identity


def live_darwin_processes(
    identities: Sequence[DarwinProcessIdentity],
) -> list[int]:
    return [
        identity[0]
        for identity in identities
        if current_darwin_process_identity(identity[0]) == identity
    ]


def darwin_child_process_identities(
    parent: DarwinProcessIdentity,
) -> tuple[DarwinProcessIdentity, ...]:
    assert _LIBPROC is not None
    assert current_darwin_process_identity(parent[0]) == parent, (
        "parent process exited before child inspection"
    )
    capacity = 16
    while True:
        child_pids = (ctypes.c_int * capacity)()
        ctypes.set_errno(0)
        count = _LIBPROC.proc_listchildpids(
            parent[0],
            child_pids,
            ctypes.sizeof(child_pids),
        )
        error = ctypes.get_errno()
        if count < 0 or (count == 0 and error != 0):
            raise OSError(error, f"failed to list children of process {parent[0]}")
        if count < capacity:
            break
        capacity *= 2

    assert current_darwin_process_identity(parent[0]) == parent, (
        "parent process changed during child inspection"
    )
    return tuple(capture_darwin_process_identity(pid) for pid in child_pids[:count])


def _darwin_process_resources(
    identity: DarwinProcessIdentity,
) -> tuple[set[tuple[int, int]], _DarwinThreadInfo] | None:
    assert _LIBPROC is not None
    if current_darwin_process_identity(identity[0]) != identity:
        return None

    proc_pidlistfds = 1
    proc_pidlistthreads = 6
    proc_pidthreadinfo = 5

    fd_infos = (_DarwinProcessFdInfo * 16)()
    fd_size = _LIBPROC.proc_pidinfo(
        identity[0],
        proc_pidlistfds,
        0,
        fd_infos,
        ctypes.sizeof(fd_infos),
    )
    if fd_size <= 0:
        return None
    assert fd_size % ctypes.sizeof(_DarwinProcessFdInfo) == 0, fd_size
    file_descriptors = {
        (info.fd, info.fdtype)
        for info in fd_infos[: fd_size // ctypes.sizeof(_DarwinProcessFdInfo)]
    }
    thread_ids = (ctypes.c_uint64 * 16)()
    thread_size = _LIBPROC.proc_pidinfo(
        identity[0],
        proc_pidlistthreads,
        0,
        thread_ids,
        ctypes.sizeof(thread_ids),
    )
    if thread_size != ctypes.sizeof(ctypes.c_uint64):
        return None

    thread_info = _DarwinThreadInfo()
    info_size = _LIBPROC.proc_pidinfo(
        identity[0],
        proc_pidthreadinfo,
        thread_ids[0],
        ctypes.byref(thread_info),
        ctypes.sizeof(thread_info),
    )
    if (
        info_size != ctypes.sizeof(thread_info)
        or current_darwin_process_identity(identity[0]) != identity
    ):
        return None
    return file_descriptors, thread_info


def _darwin_main_thread_waits(thread_info: _DarwinThreadInfo) -> bool:
    th_state_waiting = 3
    return (
        thread_info.run_state == th_state_waiting
        and thread_info.name.rstrip(b"\0") == b"main"
    )


def darwin_process_waits_for_control(
    identity: DarwinProcessIdentity,
) -> bool:
    """Return whether the exact manager process is waiting for disposition."""
    prox_fdtype_vnode = 1
    prox_fdtype_socket = 2
    resources = _darwin_process_resources(identity)
    if resources is None:
        return False
    file_descriptors, thread_info = resources
    inherited_stdin_control = file_descriptors == {
        (0, prox_fdtype_socket),
        (1, prox_fdtype_vnode),
        (2, prox_fdtype_vnode),
    }
    standard_descriptors = {
        (0, prox_fdtype_vnode),
        (1, prox_fdtype_vnode),
        (2, prox_fdtype_vnode),
    }
    inherited_extra_control = (
        standard_descriptors.issubset(file_descriptors)
        and len(
            {
                descriptor
                for descriptor, descriptor_type in file_descriptors
                if descriptor > 2 and descriptor_type == prox_fdtype_socket
            }
        )
        == 1
    )
    return (
        inherited_stdin_control or inherited_extra_control
    ) and _darwin_main_thread_waits(thread_info)


def darwin_process_waits_for_startup_release(
    identity: DarwinProcessIdentity,
) -> bool:
    """Return whether the exact target wrapper is waiting on its private gate."""
    prox_fdtype_socket = 2
    resources = _darwin_process_resources(identity)
    if resources is None:
        return False
    file_descriptors, thread_info = resources
    standard_descriptors = {
        descriptor for descriptor, _ in file_descriptors if descriptor <= 2
    }
    extra_descriptors = [
        descriptor_type
        for descriptor, descriptor_type in file_descriptors
        if descriptor > 2
    ]
    return (
        standard_descriptors == {0, 1, 2}
        and extra_descriptors == [prox_fdtype_socket]
        and _darwin_main_thread_waits(thread_info)
    )


def signal_darwin_process(identity: DarwinProcessIdentity, number: int) -> bool:
    # macOS has no pidfd-like signal API. Recheck the start time immediately
    # before signaling so a reused PID is not treated as the test process.
    if current_darwin_process_identity(identity[0]) != identity:
        return False
    try:
        os.kill(identity[0], number)
    except ProcessLookupError:
        return False
    return True


def kill_darwin_processes(
    identities: Sequence[DarwinProcessIdentity],
) -> list[int]:
    survivors = live_darwin_processes(identities)
    for identity in identities:
        signal_darwin_process(identity, signal.SIGKILL)
    return survivors


@dataclass(frozen=True)
class TranscriptWithCompanions:
    transcript: Transcript
    companions: dict[str, YamlStream | str]


class FifoCheckpoint:
    def __init__(self, path: Path) -> None:
        self.path = path
        os.mkfifo(path)
        self.descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)

    def close(self) -> None:
        os.close(self.descriptor)

    def wait(self, description: str, timeout: float = 10) -> None:
        readable, _, _ = select.select([self.descriptor], [], [], timeout)
        assert readable, f"checkpoint was not reached: {description}"
        assert os.read(self.descriptor, 1) == b"1"

    def release(self) -> None:
        assert os.write(self.descriptor, b"1") == 1


def build_manager_interposer(directory: Path) -> Path:
    source = directory / "manager-interposer.c"
    library = directory / "manager-interposer.dylib"
    source.write_text(
        r"""
#include <crt_externs.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

typedef int (*killpg_function)(pid_t, int);
typedef int (*kill_function)(pid_t, int);
typedef pid_t (*getpgid_function)(pid_t);
typedef ssize_t (*send_function)(int, const void *, size_t, int);

static _Atomic int reported_group_close = 0;
static pid_t denied_process_group = 0;
static int added_late_member = 0;
static pid_t seed_member = 0;
static pid_t late_member = 0;

static pid_t add_process_group_member(pid_t process_group);

static killpg_function next_killpg(void) {
    return killpg;
}

static kill_function next_kill(void) {
    return kill;
}

static getpgid_function next_getpgid(void) {
    return getpgid;
}

static send_function next_send(void) {
    return send;
}

static int is_manager(void) {
    int argc = *_NSGetArgc();
    char **argv = *_NSGetArgv();
    return argc > 1 && strcmp(argv[1], "sandbox-manager") == 0;
}

static void checkpoint(const char *name) {
    const char *path = getenv(name);
    if (path == NULL) {
        return;
    }
    int descriptor = open(path, O_WRONLY);
    if (descriptor < 0) {
        return;
    }
    char byte = '1';
    write(descriptor, &byte, sizeof(byte));
    close(descriptor);
}

static void write_pid_marker(const char *name, pid_t process_id) {
    const char *path = getenv(name);
    if (path == NULL) {
        return;
    }
    int descriptor = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (descriptor >= 0) {
        dprintf(descriptor, "%d\n", process_id);
        close(descriptor);
    }
}

static void write_member_marker(pid_t process_id, pid_t process_group) {
    const char *path = getenv("MCP_CONSOLE_TEST_LATE_MEMBER_MARKER");
    if (path == NULL) {
        return;
    }
    int descriptor = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (descriptor >= 0) {
        dprintf(descriptor, "%d %d\n", process_id, process_group);
        close(descriptor);
    }
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
        next_kill()(member, SIGKILL);
        while (waitpid(member, NULL, 0) < 0 && errno == EINTR) {
        }
        errno = read_error;
        return -1;
    }
    return member;
}

static ssize_t gate_manager_commit(
    int socket,
    const void *buffer,
    size_t length,
    int flags
) {
    if (is_manager()
        && socket == STDIN_FILENO
        && length == 1
        && ((const uint8_t *)buffer)[0] == 7) {
        checkpoint("MCP_CONSOLE_TEST_MANAGER_COMMITTED_READY");
        const char *release = getenv("MCP_CONSOLE_TEST_MANAGER_COMMITTED_RELEASE");
        if (release != NULL) {
            int descriptor = open(release, O_RDONLY);
            char byte;
            if (descriptor >= 0) {
                read(descriptor, &byte, sizeof(byte));
                close(descriptor);
            }
        }
    }
    send_function send_next = next_send();
    return send_next(socket, buffer, length, flags);
}

static int manager_group_close(pid_t process_group, int number) {
    if (number == SIGKILL && is_manager()) {
        if (atomic_exchange(&reported_group_close, 1) == 0) {
            checkpoint("MCP_CONSOLE_TEST_MANAGER_GROUP_CLOSED");
        }
        if (getenv("MCP_CONSOLE_TEST_KILLPG_MARKER") != NULL) {
            denied_process_group = process_group;
            // Keep one manager child in the first exact-group snapshot. Its
            // membership check adds another child after that snapshot.
            seed_member = add_process_group_member(process_group);
            if (seed_member < 0) {
                return -1;
            }
            write_pid_marker("MCP_CONSOLE_TEST_KILLPG_MARKER", process_group);
            errno = EPERM;
            return -1;
        }
    }
    killpg_function killpg_next = next_killpg();
    return killpg_next(process_group, number);
}

static pid_t getpgid_and_add_member(pid_t process_id) {
    pid_t process_group = next_getpgid()(process_id);
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

static int kill_and_reap_added_member(pid_t process_id, int number) {
    int result = next_kill()(process_id, number);
    int signal_error = errno;
    if (result == 0 && number == SIGKILL
        && (process_id == seed_member || process_id == late_member)) {
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

__attribute__((used))
static struct {
    const void *replacement;
    const void *replacee;
} interposers[] __attribute__((section("__DATA,__interpose"))) = {
    {(const void *)&gate_manager_commit, (const void *)&send},
    {(const void *)&manager_group_close, (const void *)&killpg},
    {(const void *)&getpgid_and_add_member, (const void *)&getpgid},
    {(const void *)&kill_and_reap_added_member, (const void *)&kill},
};
""".removeprefix("\n"),
        encoding="utf-8",
    )
    subprocess.run(
        [
            "cc",
            "-dynamiclib",
            "-Wall",
            "-Wextra",
            "-Werror",
            source,
            "-o",
            library,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return library


def checkpoint_uv_environment(
    temporary: Path,
    argument: str,
    *,
    reuse_resolved_python_for: tuple[str, ...] = (),
    provide_python_module: tuple[str, str] | None = None,
) -> tuple[dict[str, str], FifoCheckpoint, FifoCheckpoint]:
    assert all(reuse_resolved_python_for)
    assert provide_python_module is None or (
        provide_python_module[0] in reuse_resolved_python_for
        and provide_python_module[1].isidentifier()
    )
    real_uv = shutil.which("uv")
    assert real_uv is not None, "real uv is required"
    started = FifoCheckpoint(temporary / "uv-started")
    release = FifoCheckpoint(temporary / "uv-release")
    environment = os.environ.copy()
    environment["RETICULATE_UV"] = str(
        Path(__file__).parent.parent / "fixtures" / "checkpoint_uv"
    )
    environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
    environment["MCP_CONSOLE_TEST_UV_CHECKPOINT_ARGUMENT"] = argument
    environment["MCP_CONSOLE_TEST_UV_CHECKPOINT_CLAIM"] = str(temporary / "uv-claimed")
    environment["MCP_CONSOLE_TEST_UV_STARTED"] = str(started.path)
    environment["MCP_CONSOLE_TEST_UV_RELEASE"] = str(release.path)
    if reuse_resolved_python_for:
        environment["MCP_CONSOLE_TEST_UV_REUSE_PYTHON"] = str(
            temporary / "resolved-python"
        )
        environment["MCP_CONSOLE_TEST_UV_REUSE_REQUIREMENTS"] = os.pathsep.join(
            reuse_resolved_python_for
        )
        environment["MCP_CONSOLE_TEST_UV_REUSE_RECORD"] = str(
            temporary / "uv-reuse-record"
        )
    if provide_python_module is not None:
        requirement, module = provide_python_module
        modules = temporary / "python-modules"
        modules.mkdir()
        environment["PYTHONPATH"] = str(modules)
        environment["MCP_CONSOLE_TEST_UV_PROVIDE_REQUIREMENT"] = requirement
        environment["MCP_CONSOLE_TEST_UV_PROVIDE_MODULE"] = str(
            modules / f"{module}.py"
        )
    return environment, started, release


def code(source: str) -> str:
    return dedent(source).removeprefix("\n")


def normalize_python_resolution_error(error: str, invalid: str | None = None) -> str:
    error = normalize_python_traceback_paths(error)
    error, python_patch = re.subn(
        r'(?m)^(  "python": "\d+\.\d+)\.\d+( \(reticulate default\))?(",)$',
        r"\1.x\2\3",
        error,
        count=1,
    )
    assert python_patch == 1, error
    has_python_version = '\n  "python_version": [\n' in error
    error, python_version_patch = re.subn(
        r'(?m)^(  "python_version": \[\n    "\d+\.\d+)\.\d+("\n  \])$',
        r"\1.x\2",
        error,
        count=1,
    )
    assert python_version_patch == int(has_python_version), error
    if invalid is not None:
        assert invalid in error, error
    return error


def normalize_python_traceback_paths(error: str) -> str:
    replacements = (
        (
            r'(?m)^(\s+File ")[^"\n]*/reticulate/python/(rpytools/loader\.py")',
            r"\1<reticulate>/python/\2",
        ),
        (
            r'(?m)^(\s+File ")[^"\n]*/lib/python\d+\.\d+/(importlib/__init__\.py")',
            r"\1<python-stdlib>/\2",
        ),
        (
            r'(?m)^(\s+File ")[^"\n]*/(tests/fixtures/checkpoint_uv")'
            r", line \d+",
            r"\1<workspace>/\2, line <line>",
        ),
    )
    for pattern, replacement in replacements:
        error = re.sub(pattern, replacement, error)
    assert re.search(r'(?m)^\s+File "/', error) is None, error
    return error


def r_test_environment() -> tuple[dict[str, str], Path]:
    environment = os.environ.copy()
    if r_home := environment.get("R_HOME"):
        home = Path(r_home)
    else:
        output = subprocess.run(
            ["R", "RHOME"],
            check=True,
            capture_output=True,
            text=True,
        )
        home = Path(output.stdout.strip())
        environment["R_HOME"] = str(home)
    return environment, home / "bin" / "Rscript"


def build_r_input_handler(
    directory: Path,
    environment: dict[str, str],
    rscript: Path,
) -> None:
    source = Path(__file__).parent.parent / "fixtures" / "r_input_handler.c"
    local_source = directory / source.name
    shutil.copyfile(source, local_source)
    subprocess.run(
        [
            rscript.parent / "R",
            "CMD",
            "SHLIB",
            "-o",
            "mcp_test_input_handler.so",
            local_source.name,
        ],
        cwd=directory,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def reference_plots(
    rscript: Path,
    environment: dict[str, str],
    source: str,
    *,
    width: float,
    height: float,
    dpi: float,
    pages: int,
    expected_error: str | None = None,
) -> list[bytes]:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        error_handler = ""
        if expected_error is not None:
            message = json.dumps(expected_error)
            error_handler = (
                ", error = function(error) "
                f"stopifnot(identical(conditionMessage(error), {message}))"
            )
        script = (
            "base::local({\n"
            "  directory <- commandArgs(trailingOnly = TRUE)[[1L]]\n"
            "  device_counter <- 0L\n"
            "  options(device = function(...) {\n"
            "    device_counter <<- device_counter + 1L\n"
            "    grDevices::png(\n"
            "      filename = file.path(\n"
            "        directory,\n"
            '        sprintf("device-%06d-page-%%06d.png", device_counter)\n'
            "      ),\n"
            f'      width = {width}, height = {height}, units = "in", res = {dpi}\n'
            "    )\n"
            "  })\n"
            "  tryCatch({\n"
            f"{source}"
            f"  }}{error_handler}, finally = grDevices::graphics.off())\n"
            "})\n"
        )
        subprocess.run(
            [rscript, "--vanilla", "-", str(directory)],
            input=script,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        paths = sorted(directory.glob("device-*-page-*.png"))
        assert len(paths) == pages, paths
        return [path.read_bytes() for path in paths]


def assert_result_content(
    client: "McpClient",
    expected: list[str | bytes],
    *,
    image_reference: str = "live Rscript page {page}",
) -> None:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    content = result["content"]
    assert len(content) == len(expected), (
        f"expected {len(expected)} content blocks, got "
        f"{[item.get('type') for item in content]}"
    )
    page = 0
    for item, expected_item in zip(content, expected):
        if isinstance(expected_item, str):
            assert item == {"type": "text", "text": expected_item}, item
            continue

        image = item
        assert image.keys() == {"type", "data", "mimeType"}, image
        assert image["type"] == "image", image
        assert image["mimeType"] == "image/png", image
        data = base64.b64decode(image["data"], validate=True)
        reference = image_reference.format(page=page + 1)
        assert data == expected_item, (
            f"plot bytes differ: worker returned {len(data)} bytes, "
            f"{reference} returned {len(expected_item)} bytes"
        )
        page += 1
        image["data"] = f"<PNG byte-identical to {reference}>"


def release_worker_callback_gate(
    client: "McpClient",
    description: str,
    extra_path_labels: tuple[str, ...] = (),
) -> tuple[Path, ...]:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    content = result["content"]
    assert len(content) == 1 and content[0]["type"] == "text", content
    paths = content[0]["text"].splitlines()
    assert len(paths) == 2 + len(extra_path_labels), content
    content[0]["text"] = "\n".join(
        (
            "<worker callback gate>",
            "<worker callback checkpoint>",
            *(f"<worker callback {label}>" for label in extra_path_labels),
        )
    )

    gate, checkpoint, *extra_paths = map(Path, paths)
    gate.touch()
    deadline = time.monotonic() + 5
    while not checkpoint.exists():
        assert client.process.poll() is None, (
            f"mcp-console stopped before {description} reached its checkpoint"
        )
        assert time.monotonic() < deadline, (
            f"{description} did not reach its checkpoint"
        )
        time.sleep(0.01)
    return tuple(extra_paths)


def wait_for_idle_output(
    client: "McpClient",
    expected: str,
    description: str,
    **send_arguments: Any,
) -> None:
    """Poll the public idle snapshot until a worker event reaches the server."""
    deadline = time.monotonic() + 3
    poll_start = len(client.transcript)
    while True:
        result = client.send(**send_arguments)
        assert result.get("isError") is not True, result
        content = result["content"]
        assert len(content) == 1 and content[0]["type"] == "text", content
        output = content[0]["text"]
        if output == expected:
            break
        assert output == "\n[idle]", output
        if time.monotonic() >= deadline:
            raise AssertionError(f"{description} did not reach the server")
        time.sleep(0.01)

    polls = client.transcript[poll_start:]
    final_poll = polls[-1]
    client.transcript[poll_start:] = [final_poll]


def wait_for_evaluation_output(
    client: "McpClient",
    expected: str,
    description: str,
    *,
    provisional: str = "\n[waiting for stdin]",
    **send_arguments: Any,
) -> None:
    """Poll past one exact provisional state and retain the submitted call."""
    deadline = time.monotonic() + 3
    poll_start = len(client.transcript)
    result = client.send(**send_arguments)
    while True:
        assert result.get("isError") is not True, result
        content = result["content"]
        assert len(content) == 1 and content[0]["type"] == "text", content
        output = content[0]["text"]
        if output == expected:
            break
        assert output == provisional, repr(output)
        assert time.monotonic() < deadline, f"{description} did not complete"
        result = client.send(timeout_ms=3_000)

    calls = client.transcript[poll_start:]
    submitted = calls[0]
    submitted["result"] = calls[-1]["result"]
    client.transcript[poll_start:] = [submitted]


def collect_running_output(
    client: "McpClient",
    description: str,
    *,
    timeouts_ms: tuple[int, ...],
    initial_cuts: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Poll a running evaluation and retain its public output cuts."""
    assert timeouts_ms and all(timeout_ms > 0 for timeout_ms in timeouts_ms)
    running = "\n[running; poll with an empty send]"
    poll_start = len(client.transcript)
    cuts = [cut for cut in initial_cuts if cut]
    for attempt, timeout_ms in enumerate(timeouts_ms):
        result = client.send(timeout_ms=timeout_ms)
        assert result.get("isError") is not True, result
        content = result["content"]
        assert len(content) == 1 and content[0]["type"] == "text", content
        output = content[0]["text"]
        if output.endswith(running):
            cut = output.removesuffix(running)
            if cut:
                cuts.append(cut)
            if attempt + 1 == len(timeouts_ms):
                raise AssertionError(
                    f"{description} remained running after {len(timeouts_ms)} polls: "
                    f"collected={''.join(cuts)!r}, last={output!r}"
                )
            continue

        if output != "[done]" or not cuts:
            cuts.append(output)
        break

    collected = "".join(cuts)
    content[0]["text"] = collected
    polls = client.transcript[poll_start:]
    submitted = polls[0]
    submitted["result"] = polls[-1]["result"]
    client.transcript[poll_start:] = [submitted]
    return tuple(cuts)


def run_this_suite(suite_path: str) -> None:
    suite = Path(suite_path).resolve()
    directory = next(
        parent for parent in suite.parents if (parent / "_run.py").is_file()
    )
    root = directory.parents[1]
    suite_name = suite.relative_to(directory).with_suffix("").as_posix()
    subprocess.run([root / "scripts" / "test", suite_name], check=True)


class McpClient:
    def __init__(
        self,
        binary: Path,
        arguments: tuple[str, ...] = (),
        environment: dict[str, str] | None = None,
        current_directory: Path | None = None,
        umask: int = -1,
        pass_fds: tuple[int, ...] = (),
    ) -> None:
        self.temporary_directory = (
            tempfile.TemporaryDirectory() if current_directory is None else None
        )
        if current_directory is None:
            assert self.temporary_directory is not None
            current_directory = Path(self.temporary_directory.name)
        process = subprocess.Popen(
            [binary, *arguments],
            env=environment,
            cwd=current_directory,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            umask=umask,
            pass_fds=pass_fds,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None

        self.process = process
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.stderr = process.stderr
        self.transcript: Transcript = []
        self._next_request_id = 1
        self._issued_request_ids: set[int] = set()

    def send(self, **arguments: Any) -> ToolResult:
        return self._call_tool("send", **arguments)

    def _send_message(self, message: dict[str, Any]) -> TranscriptEntry:
        recorded_message = message.copy()
        assert recorded_message.pop("jsonrpc", None) == "2.0", message

        entry = {}
        if "id" in recorded_message:
            request_id = recorded_message.pop("id")
            assert isinstance(request_id, int), message
            assert request_id not in self._issued_request_ids, (
                f"JSON-RPC request ID was reused: {request_id}"
            )
            self._issued_request_ids.add(request_id)
            entry["id"] = request_id
        params = recorded_message.get("params")
        if (
            recorded_message.keys() == {"method", "params"}
            and recorded_message["method"] == "tools/call"
            and isinstance(params, dict)
            and params.keys() == {"name", "arguments"}
            and params["name"] == "send"
            and isinstance(params["arguments"], dict)
        ):
            entry[params["name"]] = params["arguments"]
        else:
            entry["input"] = recorded_message
        self.transcript.append(entry)
        self.stdin.write(json.dumps(message) + "\n")
        self.stdin.flush()
        return entry

    def _read_response_line(self) -> str:
        line = self.stdout.readline()
        if line:
            return line

        return_code = self.process.poll()
        standard_error = ""
        readable, _, _ = select.select([self.stderr], [], [], 0)
        if readable:
            standard_error = os.read(self.stderr.fileno(), 64 * 1024).decode(
                "utf-8",
                errors="replace",
            )
        raise AssertionError(
            "mcp-console stdout closed before replying: "
            f"return_code={return_code!r}, stderr={standard_error!r}"
        )

    def _receive(self, entry: TranscriptEntry) -> None:
        line = self._read_response_line()
        message = json.loads(line)
        assert message.pop("jsonrpc", None) == "2.0", message
        assert message.pop("id", None) == entry["id"], message
        assert message.keys() == {"result"} or message.keys() == {"error"}, message
        assert entry.keys().isdisjoint(message), message
        entry.update(message)

    def _receive_many(self, entries: list[TranscriptEntry]) -> None:
        pending = {entry["id"]: entry for entry in entries}
        assert len(pending) == len(entries), "response batch reused a request ID"
        for _ in entries:
            line = self._read_response_line()
            message = json.loads(line)
            assert message.pop("jsonrpc", None) == "2.0", message
            request_id = message.pop("id", None)
            assert request_id in pending, message
            entry = pending.pop(request_id)
            assert message.keys() == {"result"} or message.keys() == {"error"}, message
            assert entry.keys().isdisjoint(message), message
            entry.update(message)

    def _start_request(self, method: str, **params: Any) -> TranscriptEntry:
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._next_request_id,
            "method": method,
        }
        self._next_request_id += 1
        if params:
            message["params"] = params

        return self._send_message(message)

    def _request(self, method: str, **params: Any) -> TranscriptEntry:
        entry = self._start_request(method, **params)
        self._receive(entry)
        return entry

    def _notify(self, method: str, **params: Any) -> None:
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params:
            message["params"] = params

        self._send_message(message)

    def _initialize_and_list_tools(self) -> None:
        self._request(
            "initialize",
            protocolVersion="2025-11-25",
            capabilities={},
            clientInfo={
                "name": "acceptance-test",
                "version": "1.0.0",
            },
        )
        self._notify("notifications/initialized")
        self._request("tools/list")

    def _start_tool_call(self, name: str, **arguments: Any) -> TranscriptEntry:
        return self._start_request(
            "tools/call",
            name=name,
            arguments=arguments,
        )

    def _call_tool(self, name: str, **arguments: Any) -> ToolResult:
        entry = self._start_tool_call(name, **arguments)
        self._receive(entry)
        result = entry["result"]
        assert isinstance(result, dict), result
        return result

    def _start_send(self, **arguments: Any) -> TranscriptEntry:
        return self._start_tool_call("send", **arguments)

    def _finish(self) -> Transcript:
        transcript, standard_error = self._finish_with_standard_error()
        assert standard_error == "", standard_error
        return transcript

    def _finish_with_standard_error(self) -> tuple[Transcript, str]:
        self.stdin.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            stdout = executor.submit(self.stdout.read)
            stderr = executor.submit(self.stderr.read)
            return_code = self.process.wait()
            extra_output = stdout.result()
            standard_error = stderr.result()

        assert return_code == 0, standard_error
        assert extra_output == "", f"unexpected extra output: {extra_output}"
        return self.transcript, standard_error


def wait_for_worker_file(root: Path, name: str, client: McpClient) -> Path:
    deadline = time.monotonic() + 10
    while True:
        paths = list(root.glob(f"**/{name}"))
        if paths:
            assert len(paths) == 1, paths
            return paths[0]
        assert client.process.poll() is None, (
            "mcp-console stopped before worker checkpoint"
        )
        assert time.monotonic() < deadline, f"worker did not create {name}"
        time.sleep(0.01)


def stop_client(client: McpClient) -> None:
    if client.process.poll() is not None:
        return
    if not client.stdin.closed:
        client.stdin.close()
    try:
        client.process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        client.process.kill()
        client.process.wait()
