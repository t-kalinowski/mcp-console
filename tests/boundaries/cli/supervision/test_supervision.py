#!/usr/bin/env -S uv run --script

import ctypes
import os
import select
import signal
import subprocess
import sys
import tempfile
import termios
from contextlib import ExitStack
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.cli._harness import (
    _build_supervision_interposer,
    _cleanup,
    _command,
    _command_record,
    _observe_process_exit,
    _read_lines,
    _start_lifetime,
    _start_with_controlling_terminal,
    _wait_for_process_exit,
)
from support.checkpoints import FifoCheckpoint
from support.macos import (
    DarwinProcessIdentity as _ProcessIdentity,
    capture_darwin_process_identity as _capture_identity,
    kill_darwin_processes as _kill_survivors,
    live_darwin_processes,
    signal_darwin_process,
)
from support.normalization import code
from support.records import Transcript
from support.suites import run_this_suite

PLATFORMS = {"darwin"}
TIMEOUT = 10


def _child_pid_by_name(parent_pid: int, name: str) -> int:
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid=,comm="],
        capture_output=True,
        text=True,
        check=True,
        timeout=TIMEOUT,
    )
    matches = []
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=2)
        if len(fields) != 3:
            continue
        pid, parent, command = fields
        if int(parent) == parent_pid and Path(command).name == name:
            matches.append(int(pid))
    assert len(matches) == 1, (parent_pid, name, matches)
    return matches[0]


def test_retires_processx_descendants_across_sessions(binary: Path) -> Transcript:
    # The processx child starts a new session on Unix. Its lightweight Python
    # program starts the sleep grandchild in a third session, so neither
    # descendant remains in its parent's process group or session.
    # fmt: r
    script = code(r"""
        child_script <- '
        import os
        import subprocess
        import time

        grandchild = subprocess.Popen(
            ["/bin/sleep", "60"],
            start_new_session=True,
        )
        payload = (
            str(grandchild.pid)
            + os.linesep
            + os.environ["TMPDIR"]
            + os.linesep
        ).encode()
        os.write(1, payload)
        time.sleep(60)
        '

        child <- processx::process$new(
          "python",
          c("-c", child_script),
          stdout = "|",
          stderr = "2>&1",
          cleanup = FALSE
        )
        stopifnot(child$poll_io(-1)[["output"]] == "ready")
        child_output <- child$read_output_lines()
        stopifnot(length(child_output) == 2L)
        writeLines(c(
          as.character(Sys.getpid()),
          as.character(child$get_pid()),
          child_output
        ))
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

    identities: list[_ProcessIdentity] = []
    try:
        root_pid, child_pid, grandchild_pid, temporary_directory = _read_lines(
            process.stdout,
            4,
            "the sandbox root, processx descendants, and temporary directory",
        )
        pids = [int(root_pid), int(child_pid), int(grandchild_pid)]
        for pid in pids:
            identities.append(_capture_identity(pid))
        temporary_directory = Path(temporary_directory)

        assert os.getsid(pids[1]) != os.getsid(pids[0])
        assert os.getsid(pids[2]) != os.getsid(pids[1])

        process.stdin.write(b"exit\n")
        process.stdin.close()
        returncode = process.wait(timeout=TIMEOUT)
        stderr = process.stderr.read().decode("utf-8")
        survivors = _kill_survivors(identities[1:])

        assert returncode == 23, returncode
        assert stderr == "", stderr
        assert survivors == [], f"sandbox descendants survived: {survivors}"
        assert not temporary_directory.exists(), (
            f"sandbox temporary directory survived: {temporary_directory}"
        )
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
        _kill_survivors(identities)
        if not process.stdin.closed:
            process.stdin.close()
        process.stdout.close()
        process.stderr.close()

    return [
        {
            "command": ["mcp-console", *arguments],
            "exit_code": returncode,
            "stdout": (
                "<sandbox root pid>\n"
                "<processx child pid>\n"
                "<processx grandchild pid>\n"
                "<sandbox temp>\n"
            ),
            "verified_descendants": [
                "processx child outside root session",
                "detached grandchild outside processx child session",
            ],
        }
    ]


def test_waits_for_processx_crash_supervision(binary: Path) -> Transcript:
    # processx's crash supervisor observes its parent asynchronously. The
    # sandbox must not return while that supervisor and its child remain live.
    # The child ignores SIGTERM so processx's own fallback would otherwise take
    # up to five seconds before escalating to SIGKILL.
    # fmt: r
    script = code(r"""
        child_script <- '
        import signal
        import time

        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print("ready", flush=True)
        time.sleep(60)
        '

        child <- processx::process$new(
          "python",
          c("-c", child_script),
          stdout = "|",
          stderr = "2>&1",
          cleanup = FALSE,
          supervise = TRUE
        )
        stopifnot(child$poll_io(5000)[["output"]] == "ready")
        stopifnot(identical(child$read_output_lines(), "ready"))
        writeLines(c(as.character(Sys.getpid()), as.character(child$get_pid())))
        flush.console()
        Sys.sleep(60)
        """)
    arguments = ("sandbox", "--", "Rscript", "--vanilla", "-e", script)
    process = subprocess.Popen(
        [binary, *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None

    identities: list[_ProcessIdentity] = []
    try:
        root_pid, child_pid = [
            int(line)
            for line in _read_lines(
                process.stdout,
                2,
                "the sandbox root and processx child PIDs",
            )
        ]
        identities.append(_capture_identity(root_pid))
        identities.append(_capture_identity(child_pid))
        supervisor_pid = _child_pid_by_name(root_pid, "supervisor")
        identities.append(_capture_identity(supervisor_pid))
        root_group = os.getpgid(root_pid)
        assert os.getpgid(child_pid) != root_group
        assert os.getpgid(supervisor_pid) != root_group

        os.kill(root_pid, signal.SIGKILL)
        returncode = process.wait(timeout=TIMEOUT)
        stderr = process.stderr.read().decode("utf-8")
        survivors = _kill_survivors(identities[1:])

        assert returncode == 137, returncode
        assert stderr == "", stderr
        assert survivors == [], f"processx-supervised processes survived: {survivors}"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=TIMEOUT)
        _kill_survivors(identities)
        process.stdout.close()
        process.stderr.close()

    return [
        {
            "command": ["mcp-console", *arguments],
            "exit_code": returncode,
            "stdout": ("<sandbox root pid>\n<processx-supervised child pid>\n"),
            "verified_descendants": [
                "processx child outside root process group",
                "processx crash supervisor outside root process group",
            ],
        }
    ]


def test_does_not_signal_reused_descendant_identity(binary: Path) -> Transcript:
    # Preserve a process whose PID now names a different start-time identity.
    # The test cleans up the stand-in only after the public sandbox command
    # returns, so any direct signal from the manager remains observable.
    with (
        tempfile.TemporaryDirectory() as temporary_directory,
        ExitStack() as checkpoints,
    ):
        fixture_directory = Path(temporary_directory)
        descendant_observed = FifoCheckpoint.create(
            fixture_directory / "retirement-descendant-observed"
        )
        checkpoints.callback(descendant_observed.close)
        identity_changed = FifoCheckpoint.create(
            fixture_directory / "retirement-identity-changed"
        )
        checkpoints.callback(identity_changed.close)
        environment = os.environ.copy()
        environment.update(
            {
                "DYLD_INSERT_LIBRARIES": str(
                    _build_supervision_interposer(
                        fixture_directory,
                        "retirement-reused-identity",
                    )
                ),
                "MCP_CONSOLE_TEST_RETIREMENT_DESCENDANT_OBSERVED": str(
                    descendant_observed.path
                ),
                "MCP_CONSOLE_TEST_RETIREMENT_IDENTITY_CHANGED": str(
                    identity_changed.path
                ),
            }
        )
        lifetime = _start_lifetime(binary, environment)
        try:
            descendant_observed.wait("manager observation of detached descendant")
            lifetime.process.stdin.write(b"exit\n")
            lifetime.process.stdin.close()
            identity_changed.wait("descendant identity change after child snapshot")
            returncode = lifetime.process.wait(timeout=TIMEOUT)
            stderr = lifetime.process.stderr.read().decode("utf-8")

            assert returncode == 23, returncode
            assert stderr == "", stderr
            assert live_darwin_processes((lifetime.descendant,)) == [
                lifetime.descendant[0]
            ], "manager signaled a reused descendant PID"
            assert not lifetime.temporary_directory.exists(), (
                "reused descendant identity preserved the sandbox temporary directory"
            )
            return [
                _command_record(lifetime),
                {
                    "simulated_pid_reuse": (
                        "descendant start time changed after final child snapshot"
                    ),
                    "verified_no_signal": "reused descendant PID remained live",
                    "launcher_returncode": returncode,
                    "verified_removal": "sandbox temp",
                },
            ]
        finally:
            _cleanup(lifetime)


def test_descendant_exit_during_retirement_signal_is_clean(
    binary: Path,
) -> Transcript:
    # Hold the manager immediately before kill(2), stop the exact descendant,
    # and then let the manager observe ESRCH from its original signal attempt.
    with (
        tempfile.TemporaryDirectory() as temporary_directory,
        ExitStack() as checkpoints,
    ):
        fixture_directory = Path(temporary_directory)
        descendant_observed = FifoCheckpoint.create(
            fixture_directory / "retirement-descendant-observed"
        )
        checkpoints.callback(descendant_observed.close)
        signal_gate = FifoCheckpoint.create(
            fixture_directory / "retirement-signal-gate"
        )
        checkpoints.callback(signal_gate.close)
        signal_release = FifoCheckpoint.create(
            fixture_directory / "retirement-signal-release"
        )
        checkpoints.callback(signal_release.close)
        environment = os.environ.copy()
        environment.update(
            {
                "DYLD_INSERT_LIBRARIES": str(
                    _build_supervision_interposer(
                        fixture_directory,
                        "retirement-exit-race",
                    )
                ),
                "MCP_CONSOLE_TEST_RETIREMENT_DESCENDANT_OBSERVED": str(
                    descendant_observed.path
                ),
                "MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_GATE": str(signal_gate.path),
                "MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_RELEASE": str(signal_release.path),
            }
        )
        lifetime = _start_lifetime(binary, environment)
        signal_released = False
        try:
            descendant_observed.wait("manager observation of detached descendant")
            with _observe_process_exit(lifetime.descendant) as descendant_exit:
                lifetime.process.stdin.write(b"exit\n")
                lifetime.process.stdin.close()
                signal_gate.wait("manager descendant signal")
                assert signal_darwin_process(lifetime.descendant, signal.SIGKILL), (
                    "detached descendant exited before signal-race injection"
                )
                events = descendant_exit.control(None, 1, TIMEOUT)
                assert events, "detached descendant did not exit"
                assert events[0].ident == lifetime.descendant[0], events[0]
                assert events[0].filter == select.KQ_FILTER_PROC, events[0]
                assert events[0].fflags & select.KQ_NOTE_EXIT, events[0]
                signal_release.release()
                signal_released = True

            returncode = lifetime.process.wait(timeout=TIMEOUT)
            stderr = lifetime.process.stderr.read().decode("utf-8")
            _wait_for_process_exit(
                (lifetime.root, lifetime.descendant, lifetime.manager),
                "sandbox processes survived descendant signal race",
            )

            assert returncode == 23, returncode
            assert stderr == "", stderr
            assert not lifetime.temporary_directory.exists(), (
                "descendant signal race preserved the sandbox temporary directory"
            )
            return [
                _command_record(lifetime),
                {
                    "retirement_race": (
                        "detached descendant exited immediately before manager SIGKILL"
                    ),
                    "manager_signal_result": "ESRCH",
                    "launcher_returncode": returncode,
                    "verified_cleanup": "sandbox root, detached descendant, and manager",
                    "verified_removal": "sandbox temp",
                },
            ]
        finally:
            if not signal_released:
                signal_release.release()
            _cleanup(lifetime)


def test_relays_interrupt_then_retires_descendants(binary: Path) -> Transcript:
    # fmt: r
    script = code(r"""
        child <- processx::process$new(
          "/bin/sleep",
          "60",
          cleanup = FALSE
        )
        tryCatch(
          {
            writeLines(c(
              as.character(Sys.getpid()),
              as.character(child$get_pid())
            ))
            flush.console()
            Sys.sleep(60)
          },
          interrupt = function(...) {
            quit(save = "no", status = 130L, runLast = FALSE)
          }
        )
        """)
    arguments = ("sandbox", "--", "Rscript", "--vanilla", "-e", script)
    process = subprocess.Popen(
        [binary, *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None

    identities: list[_ProcessIdentity] = []
    try:
        pids = [
            int(line)
            for line in _read_lines(
                process.stdout,
                2,
                "the sandbox root and processx descendant PIDs",
            )
        ]
        identities = [_capture_identity(pid) for pid in pids]
        assert os.getpgid(pids[0]) == pids[0]
        assert os.getpgid(pids[1]) != os.getpgid(pids[0])
        os.kill(process.pid, signal.SIGINT)
        returncode = process.wait(timeout=TIMEOUT)
        stderr = process.stderr.read().decode("utf-8")
        survivors = _kill_survivors(identities)

        assert returncode == 130, returncode
        assert stderr == "", stderr
        assert survivors == [], f"interrupted sandbox processes survived: {survivors}"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=TIMEOUT)
        _kill_survivors(identities)
        process.stdout.close()
        process.stderr.close()

    return [
        {
            "command": _command(*arguments),
            "stdout": "<sandbox root pid>\n<processx child pid>\n",
            "verified_descendant": "processx child outside root process group",
        },
        {
            "signal": "SIGINT",
            "exit_code": returncode,
            "stderr": stderr,
        },
    ]


def test_sandbox_cannot_retain_its_temporary_directory(binary: Path) -> Transcript:
    # fmt: python
    sandboxed_script = code(r"""
        import os
        from pathlib import Path

        temporary_directory = Path(os.environ["TMPDIR"])
        (temporary_directory / ".mcp-console-preserve").write_text("retain\n")
        print(temporary_directory)
        """)
    arguments = ("sandbox", "--", "python", "-c", sandboxed_script)
    result = subprocess.run(
        [binary, *arguments],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )

    temporary_directory = Path(result.stdout.strip())
    assert result.returncode == 0, result
    assert result.stderr == "", result.stderr
    assert not temporary_directory.exists(), (
        f"sandbox retained its host-owned temporary directory: {temporary_directory}"
    )
    return [
        {
            "command": _command(*arguments),
            "stdout": "<sandbox temp>\n",
            "transcript_normalization": {
                "target": "stdout",
                "sandbox_temporary_directory": "omitted",
            },
        }
    ]


def test_delivers_terminal_interrupt_once(binary: Path) -> Transcript:
    # fmt: python
    sandboxed_script = code(r"""
        import os
        import signal
        import threading

        interrupts = 0
        interrupted = threading.Event()


        def handle_interrupt(_signal, _frame):
            global interrupts
            interrupts += 1
            interrupted.set()


        signal.signal(signal.SIGINT, handle_interrupt)
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        assert signal.SIGINT not in previous_mask
        print(f"ready {os.getpid()} {os.getpgrp()}", flush=True)
        print(input(), flush=True)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        print("interrupt ready", flush=True)
        interrupted.wait()
        print(interrupts)
        """)
    arguments = [binary, "sandbox", "--", "python", "-c", sandboxed_script]
    process, master, _ = _start_with_controlling_terminal(arguments)
    identities: list[_ProcessIdentity] = []
    try:
        readiness = _read_lines(
            process.stdout,
            1,
            "the foreground sandbox readiness",
        )[0].split()
        assert len(readiness) == 3 and readiness[0] == "ready", readiness
        target_pid, target_group = map(int, readiness[1:])
        identities.append(_capture_identity(target_pid))
        assert target_group == target_pid
        assert target_group != process.pid
        assert os.tcgetpgrp(master) == target_group

        os.write(master, b"sandbox input\n")
        assert _read_lines(process.stdout, 2, "the sandbox input acknowledgement") == [
            "sandbox input",
            "interrupt ready",
        ]
        assert os.tcgetpgrp(master) == target_group
        terminal_attributes = termios.tcgetattr(master)
        assert terminal_attributes[3] & termios.ISIG
        assert terminal_attributes[6][termios.VINTR] == b"\x03"
        os.write(master, b"\x03")
        stdout, stderr = process.communicate(timeout=TIMEOUT)
        survivors = _kill_survivors(identities)

        assert process.returncode == 0, process.returncode
        assert stdout == b"1\n", stdout
        assert stderr == b"", stderr
        assert survivors == [], f"terminal sandbox survived: {survivors}"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=TIMEOUT)
        _kill_survivors(identities)
        process.stdout.close()
        process.stderr.close()
        os.close(master)

    return [
        {
            "command": _command("sandbox", "--", "python", "-c", sandboxed_script),
            "stdin": "sandbox input\n<Ctrl-C>",
            "stdout": stdout.decode("utf-8"),
            "terminal_ownership": "transferred to target",
        }
    ]


def test_preserves_terminal_ownership_with_foreground_peer(binary: Path) -> Transcript:
    # A foreground shell pipeline places all of its stages in one process group.
    # Model another stage with a sibling that remains in the launcher's group.
    # fmt: python
    wrapper_script = code(r"""
        import os
        import sys

        peer = os.fork()
        if peer == 0:
            os.closerange(0, 3)
            os.execl("/bin/sleep", "sleep", "60")
        os.execv(
            sys.argv[1],
            [
                sys.argv[1],
                "sandbox",
                "--",
                "python",
                "-c",
                sys.argv[2],
                sys.argv[3],
                str(peer),
            ],
        )
        """)
    # fmt: python
    sandboxed_script = code(r"""
        import os
        import sys

        print(
            f"ready {os.getpid()} {os.getpgrp()} {sys.argv[2]}",
            flush=True,
        )
        with open(sys.argv[1], encoding="utf-8") as release:
            assert release.readline() == "release\n"
        """)

    with tempfile.TemporaryDirectory() as directory:
        release = os.path.join(directory, "release")
        os.mkfifo(release)
        process, master, _ = _start_with_controlling_terminal(
            [sys.executable, "-c", wrapper_script, binary, sandboxed_script, release]
        )

        identities: list[_ProcessIdentity] = []
        release_descriptor = None
        try:
            readiness = _read_lines(
                process.stdout,
                1,
                "the foreground-peer sandbox readiness",
            )[0].split()
            assert len(readiness) == 4 and readiness[0] == "ready", readiness
            target_pid, target_group, peer_pid = map(int, readiness[1:])
            foreground_group = os.tcgetpgrp(master)
            identities.append(_capture_identity(target_pid))
            peer_identity = _capture_identity(peer_pid)
            identities.append(peer_identity)

            assert os.getpgid(process.pid) == process.pid
            assert os.getpgid(peer_pid) == process.pid
            assert target_group == target_pid
            assert foreground_group == process.pid
            assert target_group != foreground_group

            assert _kill_survivors([peer_identity]) == [peer_pid]
            identities.pop()
            release_descriptor = os.open(release, os.O_RDWR | os.O_NONBLOCK)
            os.write(release_descriptor, b"release\n")
            returncode = process.wait(timeout=TIMEOUT)
            stderr = process.stderr.read().decode("utf-8")
            survivors = _kill_survivors(identities)

            assert returncode == 0, returncode
            assert stderr == "", stderr
            assert survivors == [], f"foreground-peer sandbox survived: {survivors}"
        finally:
            if release_descriptor is not None:
                os.close(release_descriptor)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=TIMEOUT)
            _kill_survivors(identities)
            process.stdout.close()
            process.stderr.close()
            os.close(master)

    return [
        {
            "command": _command(
                "sandbox",
                "--",
                "python",
                "-c",
                sandboxed_script,
                "<release gate>",
                "<peer pid>",
            ),
            "foreground_peer": "shares launcher process group",
            "target_process_group": "dedicated",
            "terminal_foreground_group": "launcher and peer",
            "exit_code": returncode,
        }
    ]


def test_preserves_status_after_terminal_closes(binary: Path) -> Transcript:
    # fmt: python
    sandboxed_script = code(r"""
        import os
        import signal
        import sys

        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        print("ready", flush=True)
        with open(sys.argv[1], encoding="utf-8") as release:
            assert release.readline() == "release\n"
        raise SystemExit(23)
        """)
    with tempfile.TemporaryDirectory() as directory:
        release = os.path.join(directory, "release")
        os.mkfifo(release)
        arguments = [
            binary,
            "sandbox",
            "--",
            "python",
            "-c",
            sandboxed_script,
            release,
        ]
        process, master, slave_name = _start_with_controlling_terminal(arguments)
        identities: list[_ProcessIdentity] = []
        release_descriptor = None
        try:
            assert _read_lines(process.stdout, 1, "the terminal-close readiness") == [
                "ready"
            ]
            target_pid = os.tcgetpgrp(master)
            identities.append(_capture_identity(target_pid))

            libc = ctypes.CDLL(None, use_errno=True)
            assert libc.revoke(slave_name.encode()) == 0
            os.close(master)
            master = None
            release_descriptor = os.open(release, os.O_RDWR | os.O_NONBLOCK)
            os.write(release_descriptor, b"release\n")
            stdout, stderr = process.communicate(timeout=TIMEOUT)
            survivors = _kill_survivors(identities)

            assert process.returncode == 23, process.returncode
            assert stdout == b"", stdout
            assert stderr == b"", stderr
            assert survivors == [], f"terminal-close sandbox survived: {survivors}"
        finally:
            if release_descriptor is not None:
                os.close(release_descriptor)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=TIMEOUT)
            _kill_survivors(identities)
            process.stdout.close()
            process.stderr.close()
            if master is not None:
                os.close(master)

    return [
        {
            "command": _command(
                "sandbox",
                "--",
                "python",
                "-c",
                sandboxed_script,
                "<release gate>",
            ),
            "terminal": "closed after readiness",
            "exit_code": process.returncode,
        }
    ]


def test_preserves_status_when_sigchld_was_ignored(binary: Path) -> Transcript:
    # Darwin preserves the ignored disposition across exec but clears its
    # no-child-wait state. Exercise the real binary entry point so later
    # supervision changes continue to preserve the command's waitable status.
    # fmt: python
    host_script = code(r"""
        import os
        import signal
        import sys

        signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        os.execv(sys.argv[1], sys.argv[1:])
        """)
    arguments = (
        "sandbox",
        "--",
        "python",
        "-c",
        "raise SystemExit(23)",
    )
    result = subprocess.run(
        [sys.executable, "-c", host_script, binary, *arguments],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )

    assert result.returncode == 23, result
    assert result.stdout == "", result.stdout
    assert result.stderr == "", result.stderr
    return [
        {
            "command": ["mcp-console", *arguments],
            "inherited_sigchld": "ignored",
            "exit_code": result.returncode,
        }
    ]


if __name__ == "__main__":
    run_this_suite(__file__)
