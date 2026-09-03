#!/usr/bin/env -S uv run --script

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cli._harness import (
    DarwinProcessIdentity,
    FifoCheckpoint,
    Path,
    TIMEOUT,
    Transcript,
    _build_supervision_interposer,
    _cleanup,
    _command,
    _command_record,
    _observe_process_exit,
    _start_lifetime,
    _wait_for_cleanup,
    _wait_for_gated_root_and_manager,
    _wait_for_process_exit,
    _wait_for_process_state,
    capture_darwin_process_identity,
    kill_darwin_processes,
    live_darwin_processes,
    os,
    run_this_suite,
    shutil,
    signal,
    signal_darwin_process,
    subprocess,
    sys,
    tempfile,
)


PLATFORMS = {"darwin"}


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


if __name__ == "__main__":
    run_this_suite(__file__)
