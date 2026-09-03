#!/usr/bin/env -S uv run --script

import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.cli._harness import (
    TIMEOUT,
    _assert_launcher_cleanup_barrier,
    _cleanup,
    _command_record,
    _manager_pid,
    _read_lines,
    _start_lifetime,
    _wait_for_cleanup,
    _wait_for_process_exit,
    _watch_process_exits,
)
from support.macos import (
    capture_darwin_process_identity,
    kill_darwin_processes,
    signal_darwin_process,
)
from support.checkpoints import FifoCheckpoint
from support.normalization import code
from support.records import Transcript
from support.suites import run_this_suite


PLATFORMS = {"darwin"}


def _build_startup_interposer(
    directory: Path,
    fixture_name: str,
    *compiler_flags: str,
) -> Path:
    source = directory / f"{fixture_name}.c"
    library = directory / f"{fixture_name}.dylib"
    fixture = (
        Path(__file__).resolve().parents[3]
        / "fixtures"
        / "native"
        / f"{fixture_name}.c"
    )
    shutil.copyfile(fixture, source)
    subprocess.run(
        [
            "cc",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Wpedantic",
            "-Werror",
            *compiler_flags,
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


def _sandbox_root_pid(launcher_pid: int) -> int:
    processes = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid=,command="],
        check=True,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    ).stdout
    roots = []
    for process in processes.splitlines():
        fields = process.strip().split(maxsplit=2)
        if (
            len(fields) == 3
            and int(fields[1]) == launcher_pid
            and "sandbox-target" in fields[2].split()
        ):
            roots.append(int(fields[0]))
    assert len(roots) == 1, roots
    return roots[0]


def _start_owned_echo_owner(
    binary: Path,
    environment: dict[str, str],
) -> subprocess.Popen[bytes]:
    # fmt: python
    owner_script = code(r"""
        import os
        import subprocess
        import sys

        launcher = subprocess.Popen(
            [
                sys.argv[1],
                "sandbox",
                "--exit-with-parent",
                str(os.getpid()),
                "--",
                "/bin/echo",
                "target ran",
            ],
            stdin=subprocess.DEVNULL,
        )
        print(launcher.pid, flush=True)
        raise SystemExit(launcher.wait())
        """)
    return subprocess.Popen(
        [sys.executable, "-c", owner_script, binary],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_owner_loss_before_exit_watch_cleans_startup(binary: Path) -> Transcript:
    # Gate the launcher's first kqueue after the root is spawned but before the
    # owner watch is registered. The target remains behind its startup gate.
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        waiter_started = FifoCheckpoint.create(temporary / "waiter-started")
        waiter_release = FifoCheckpoint.create(temporary / "waiter-release")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_ROOT_WAITER_START"] = str(waiter_started.path)
        environment["MCP_CONSOLE_TEST_ROOT_WAITER_RELEASE"] = str(waiter_release.path)
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_startup_interposer(
                temporary,
                "root_waiter_start_interposer",
                "-Wno-deprecated-declarations",
            )
        )
        owner = _start_owned_echo_owner(binary, environment)
        assert owner.stdout is not None
        assert owner.stderr is not None

        identities = []
        released = False
        exit_events = select.kqueue()
        try:
            (launcher_pid,) = _read_lines(
                owner.stdout,
                1,
                "the owned sandbox launcher",
            )
            waiter_started.wait("root waiter startup")
            launcher = capture_darwin_process_identity(int(launcher_pid))
            root = capture_darwin_process_identity(_sandbox_root_pid(launcher[0]))
            identities = [launcher, root]
            exit_events.close()
            exit_events, watches = _watch_process_exits((root, launcher))
            private_directories = list(temporary.glob("mcp-console-tmp-*"))
            assert len(private_directories) == 1, private_directories

            owner_identity = capture_darwin_process_identity(owner.pid)
            assert signal_darwin_process(owner_identity, signal.SIGKILL), (
                "sandbox owner exited before crash injection"
            )
            owner_returncode = owner.wait(timeout=TIMEOUT)
            waiter_release.release()
            released = True

            observed_exits = _assert_launcher_cleanup_barrier(
                exit_events,
                watches,
                launcher,
                (root,),
                private_directories[0],
                "startup",
            )
            assert observed_exits == {root[0], launcher[0]}, observed_exits
            _wait_for_process_exit(
                tuple(identities),
                "owned sandbox startup survived owner loss",
            )
            target_stdout = owner.stdout.read().decode("utf-8")
            stderr = owner.stderr.read().decode("utf-8")

            assert owner_returncode == -signal.SIGKILL, owner_returncode
            assert stderr == (
                f"sandbox owner {owner.pid} exited before exit observation\n"
            ), stderr
            assert target_stdout == "", target_stdout
            assert not list(temporary.glob("mcp-console-tmp-*")), (
                "owned sandbox startup preserved its private directory"
            )
            return [
                {
                    "command": [
                        "mcp-console",
                        "sandbox",
                        "--exit-with-parent",
                        "<owner pid>",
                        "--",
                        "/bin/echo",
                        "target ran",
                    ],
                    "owner_signal": "SIGKILL before owner-watch registration",
                    "owner_returncode": owner_returncode,
                    "stderr": (
                        "sandbox owner <owner pid> exited before exit observation\n"
                    ),
                    "verified_target": "did not run",
                    "verified_cleanup": "sandbox root and private directory",
                }
            ]
        finally:
            if not released:
                waiter_release.release()
            if owner.poll() is None:
                owner.kill()
                owner.wait(timeout=TIMEOUT)
            kill_darwin_processes(identities)
            if identities:
                _wait_for_process_exit(
                    tuple(identities),
                    "owned sandbox startup cleanup did not stop all processes",
                )
            exit_events.close()
            waiter_started.close()
            waiter_release.close()
            owner.stdout.close()
            owner.stderr.close()


def test_owner_loss_before_target_release_cancels_startup(binary: Path) -> Transcript:
    # The manager reaches its own startup entry point only after the launcher
    # has registered the owner watch. Hold readiness there, then remove the
    # owner so the final identity check must keep the target gated.
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        manager_started = FifoCheckpoint.create(temporary / "manager-started")
        manager_release = FifoCheckpoint.create(temporary / "manager-release")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_MANAGER_START"] = str(manager_started.path)
        environment["MCP_CONSOLE_TEST_MANAGER_RELEASE"] = str(manager_release.path)
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_startup_interposer(temporary, "manager_start_interposer")
        )
        owner = _start_owned_echo_owner(binary, environment)
        assert owner.stdout is not None
        assert owner.stderr is not None

        identities = []
        released = False
        exit_events = select.kqueue()
        try:
            (launcher_pid,) = _read_lines(
                owner.stdout,
                1,
                "the owned sandbox launcher",
            )
            manager_started.wait("manager startup")
            launcher = capture_darwin_process_identity(int(launcher_pid))
            root = capture_darwin_process_identity(_sandbox_root_pid(launcher[0]))
            manager = capture_darwin_process_identity(_manager_pid(launcher[0]))
            identities = [launcher, root, manager]
            exit_events.close()
            cleanup = (root, manager)
            exit_events, watches = _watch_process_exits((*cleanup, launcher))
            private_directories = list(temporary.glob("mcp-console-tmp-*"))
            assert len(private_directories) == 1, private_directories

            owner_identity = capture_darwin_process_identity(owner.pid)
            assert signal_darwin_process(owner_identity, signal.SIGKILL), (
                "sandbox owner exited before crash injection"
            )
            owner_returncode = owner.wait(timeout=TIMEOUT)
            manager_release.release()
            released = True

            observed_exits = _assert_launcher_cleanup_barrier(
                exit_events,
                watches,
                launcher,
                cleanup,
                private_directories[0],
                "startup",
            )
            assert observed_exits == {identity[0] for identity in (*cleanup, launcher)}
            _wait_for_process_exit(
                tuple(identities),
                "owned sandbox startup survived owner loss",
            )
            target_stdout = owner.stdout.read().decode("utf-8")
            stderr = owner.stderr.read().decode("utf-8")

            assert owner_returncode == -signal.SIGKILL, owner_returncode
            assert stderr == (
                f"sandbox owner {owner.pid} changed before target release\n"
            ), stderr
            assert target_stdout == "", target_stdout
            assert not list(temporary.glob("mcp-console-tmp-*")), (
                "owned sandbox startup preserved its private directory"
            )
            return [
                {
                    "command": [
                        "mcp-console",
                        "sandbox",
                        "--exit-with-parent",
                        "<owner pid>",
                        "--",
                        "/bin/echo",
                        "target ran",
                    ],
                    "owner_signal": "SIGKILL before target release",
                    "owner_returncode": owner_returncode,
                    "stderr": "sandbox owner <owner pid> changed before target release\n",
                    "verified_target": "did not run",
                    "verified_cleanup": "sandbox root, manager, and private directory",
                }
            ]
        finally:
            if not released:
                manager_release.release()
            if owner.poll() is None:
                owner.kill()
                owner.wait(timeout=TIMEOUT)
            kill_darwin_processes(identities)
            if identities:
                _wait_for_process_exit(
                    tuple(identities),
                    "owned sandbox startup cleanup did not stop all processes",
                )
            exit_events.close()
            manager_started.close()
            manager_release.close()
            owner.stdout.close()
            owner.stderr.close()


def test_owner_loss_retires_the_sandbox_lifetime(binary: Path) -> Transcript:
    # Keep the target behind its inherited stdin until the owner has reported
    # the launcher PID. The detached child then leaves the target's session, so
    # owner-loss cleanup must come from manager observation rather than a
    # process-group signal.
    # fmt: python
    target_script = code(r"""
        import os
        import subprocess
        import time

        assert input() == "start"
        child = subprocess.Popen(
            ["/bin/sleep", "60"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        print(os.getpid())
        print(child.pid)
        print(os.environ["TMPDIR"], flush=True)
        time.sleep(60)
        """)
    # fmt: python
    owner_script = code(r"""
        import os
        import subprocess
        import sys

        launcher = subprocess.Popen(
            [
                sys.argv[1],
                "sandbox",
                "--exit-with-parent",
                str(os.getpid()),
                "--",
                "python",
                "-c",
                sys.argv[2],
            ],
            stdin=subprocess.PIPE,
        )
        assert launcher.stdin is not None
        print(launcher.pid, flush=True)
        launcher.stdin.write(b"start\n")
        launcher.stdin.close()
        raise SystemExit(launcher.wait())
        """)
    owner = subprocess.Popen(
        [sys.executable, "-c", owner_script, binary, target_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert owner.stdout is not None
    assert owner.stderr is not None

    identities = []
    exit_events = select.kqueue()
    temporary_directory: Path | None = None
    try:
        launcher_pid, root_pid, descendant_pid, temporary_directory_text = _read_lines(
            owner.stdout,
            4,
            "the sandbox launcher, root, descendant, and temporary directory",
        )
        owner_identity = capture_darwin_process_identity(owner.pid)
        launcher = capture_darwin_process_identity(int(launcher_pid))
        root = capture_darwin_process_identity(int(root_pid))
        descendant = capture_darwin_process_identity(int(descendant_pid))
        manager = capture_darwin_process_identity(_manager_pid(launcher[0]))
        identities = [launcher, root, descendant, manager]
        temporary_directory = Path(temporary_directory_text)
        assert os.getsid(descendant[0]) != os.getsid(root[0]), (
            "sandbox descendant did not leave the target session"
        )
        exit_events.close()
        cleanup = (root, descendant, manager)
        exit_events, watches = _watch_process_exits((*cleanup, launcher))

        assert signal_darwin_process(owner_identity, signal.SIGKILL), (
            "sandbox owner exited before crash injection"
        )
        returncode = owner.wait(timeout=TIMEOUT)

        observed_exits = _assert_launcher_cleanup_barrier(
            exit_events,
            watches,
            launcher,
            cleanup,
            temporary_directory,
            "owner-loss",
        )
        assert observed_exits == {identity[0] for identity in (*cleanup, launcher)}
        _wait_for_process_exit(
            tuple(identities),
            "owned sandbox lifetime remained after launcher exit",
        )
        stderr = owner.stderr.read().decode("utf-8")

        assert returncode == -signal.SIGKILL, returncode
        assert stderr == "", stderr
        assert not temporary_directory.exists(), (
            "owned sandbox launcher exited before removing its temporary directory"
        )
        return [
            {
                "command": [
                    "mcp-console",
                    "sandbox",
                    "--exit-with-parent",
                    "<owner pid>",
                    "--",
                    "python",
                    "-c",
                    target_script,
                ],
                "stdout": (
                    "<sandbox root pid>\n<detached descendant pid>\n<sandbox temp>\n"
                ),
            },
            {
                "owner_signal": "SIGKILL",
                "owner_returncode": returncode,
                "verified_cleanup_barrier": (
                    "launcher exited after sandbox root/target, detached descendant, "
                    "manager, and temp"
                ),
            },
        ]
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=TIMEOUT)
        kill_darwin_processes(identities)
        if identities:
            _wait_for_process_exit(
                tuple(identities),
                "owned sandbox cleanup did not stop all processes",
            )
        if temporary_directory is not None:
            shutil.rmtree(temporary_directory, ignore_errors=True)
        exit_events.close()
        owner.stdout.close()
        owner.stderr.close()


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
        assert signal_darwin_process(lifetime.manager, signal.SIGTERM), (
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
                "manager_signal": "SIGTERM",
                "launcher_returncode": returncode,
                "verified_cleanup": "sandbox root, detached descendant, and manager",
                "verified_preservation": "sandbox temp",
            },
        ]
    finally:
        _cleanup(lifetime)


if __name__ == "__main__":
    run_this_suite(__file__)
