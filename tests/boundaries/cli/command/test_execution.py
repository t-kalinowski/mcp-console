#!/usr/bin/env -S uv run --script

import os
import pty
import selectors
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.normalization import code
from support.records import Transcript, TranscriptEntry
from support.suites import run_this_suite


PLATFORMS = {"darwin"}


def record(
    binary: Path,
    *arguments: str,
    environment: dict[str, str | None] | None = None,
    current_directory: Path | None = None,
) -> TranscriptEntry:
    child_environment = os.environ.copy()
    for name, value in (environment or {}).items():
        if value is None:
            child_environment.pop(name, None)
        else:
            child_environment[name] = value

    result = subprocess.run(
        [binary, *arguments],
        capture_output=True,
        cwd=current_directory,
        env=child_environment,
    )
    entry: TranscriptEntry = {
        "command": ["mcp-console", *arguments],
    }
    if environment is not None:
        entry["environment"] = environment
    if result.returncode != 0:
        entry["exit_code"] = result.returncode
    entry["stdout"] = result.stdout.decode("utf-8")
    if result.stderr:
        entry["stderr"] = result.stderr.decode("utf-8")
    return entry


def test_preserves_arguments_and_executable_names(binary: Path) -> Transcript:
    transcript: Transcript = []

    with TemporaryDirectory() as directory:
        current_directory = Path(directory)
        shutil.copy("/usr/bin/true", current_directory / "program=fixture")
        entry = record(
            binary,
            "sandbox",
            "--",
            "./program=fixture",
            "/usr/bin/false",
            current_directory=current_directory,
        )

    assert "exit_code" not in entry, (
        "the executable name was parsed as an environment assignment"
    )
    transcript.append({"scenario": "equals sign in executable name", **entry})

    with TemporaryDirectory() as directory:
        current_directory = Path(directory)
        shutil.copy("/usr/bin/true", current_directory / "--help")
        entry = record(
            binary,
            "sandbox",
            "--",
            "--help",
            environment={"PATH": "."},
            current_directory=current_directory,
        )

    assert "exit_code" not in entry, (
        "the executable name was parsed as a launcher option"
    )
    assert entry["stdout"] == "", "the launcher handled the target's option-like name"
    transcript.append({"scenario": "option-like executable name", **entry})

    # fmt: python
    script = code(r"""
        import sys

        print("|".join(sys.argv[1:]))
        """)
    arguments = (
        "sandbox",
        "python",
        "-c",
        script,
        "hello world",
        "$(not-a-command)",
        "--child-option",
    )
    transcript.append(
        {
            "scenario": "literal arguments and standard output",
            **record(binary, *arguments),
        }
    )
    return transcript


def test_forwards_interactive_standard_streams(binary: Path) -> Transcript:
    # fmt: python
    script = code(r"""
        import sys

        for line in sys.stdin:
            if line == "EXIT\n":
                break

            sys.stdout.write(line)
            sys.stdout.flush()
            sys.stderr.write(line)
            sys.stderr.flush()
        """)
    arguments = ("sandbox", "--", "python", "-c", script)

    process = subprocess.Popen(
        [binary, *arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stdin = process.stdin
    stdout = process.stdout
    stderr = process.stderr
    timeout = 5

    try:
        input_line = b"echo exactly: $(literal)\n"
        stdin.write(input_line)
        stdin.flush()

        output = {"stdout": bytearray(), "stderr": bytearray()}
        with selectors.DefaultSelector() as selector:
            selector.register(stdout, selectors.EVENT_READ, "stdout")
            selector.register(stderr, selectors.EVENT_READ, "stderr")
            deadline = time.monotonic() + timeout
            while any(b"\n" not in stream for stream in output.values()):
                remaining = deadline - time.monotonic()
                assert remaining > 0, "timed out waiting for sandbox output"
                ready = selector.select(remaining)
                assert ready, "timed out waiting for sandbox output"
                for key, _ in ready:
                    chunk = os.read(key.fd, 4096)
                    assert chunk, f"{key.data} closed before returning a line"
                    output[key.data].extend(chunk)

        echoed_output = output["stdout"].decode("utf-8")
        echoed_error = output["stderr"].decode("utf-8")
        input_text = input_line.decode("utf-8")

        assert process.poll() is None
        assert echoed_output == input_text
        assert echoed_error == input_text

        stdin.write(b"EXIT\n")
        stdin.flush()
        exit_code = process.wait(timeout=timeout)
        assert os.read(stdout.fileno(), 4096) == b""
        assert os.read(stderr.fileno(), 4096) == b""
        assert exit_code == 0
    finally:
        try:
            try:
                stdin.close()
            except BrokenPipeError:
                pass
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=timeout)
        finally:
            stdout.close()
            stderr.close()

    return [
        {
            "command": ["mcp-console", *arguments],
            "stdin": input_text,
            "stdout": echoed_output,
            "stderr": echoed_error,
        },
        {
            "stdin": "EXIT\n",
            "exit_code": exit_code,
        },
    ]


def test_enforces_host_read_only_and_temporary_writes(binary: Path) -> Transcript:
    # fmt: python
    script = code(r"""
        import errno
        import os
        import pathlib
        import sys

        host_file = pathlib.Path(sys.argv[1])
        assert host_file.read_text(encoding="utf-8") == "host data"
        try:
            host_file.write_text("modified", encoding="utf-8")
        except OSError as error:
            assert error.errno == errno.EPERM
        else:
            raise SystemExit("host regular-file write was allowed")

        output = pathlib.Path(os.environ["TMPDIR"]) / "result.txt"
        output.write_text("sandbox temp", encoding="utf-8")
        print(host_file.read_text(encoding="utf-8"))
        print(output.read_text(encoding="utf-8"))
        print(os.environ["TMPDIR"])
        """)

    with TemporaryDirectory() as directory:
        current_directory = Path(directory)
        host_file = current_directory / "host.txt"
        host_file.write_text("host data", encoding="utf-8")
        entry = record(
            binary,
            "sandbox",
            "--",
            "python",
            "-c",
            script,
            "host.txt",
            current_directory=current_directory,
        )
        stdout = entry["stdout"]
        assert isinstance(stdout, str)
        host_data, temporary_data, temporary_directory = stdout.splitlines()
        assert host_data == "host data"
        assert temporary_data == "sandbox temp"
        assert host_file.read_text(encoding="utf-8") == "host data"
        assert not Path(temporary_directory).exists()

    entry["stdout"] = "host data\nsandbox temp\n<sandbox temp>\n"
    entry["transcript_normalization"] = {
        "target": "stdout line 3",
        "sandbox_temporary_directory": "omitted",
    }
    return [entry]


def test_allows_processx_pty_processes(binary: Path) -> Transcript:
    # fmt: r
    script = code(r"""
        {
          p <- processx::process$new("/bin/cat", pty = TRUE)
          on.exit(if (p$is_alive()) p$kill())
          p$write_input("sandboxed pty\n")
          stopifnot(p$poll_io(5000)[["output"]] == "ready")
          cat(p$read_output())
          invisible(p$kill())
        }
        """)
    return [record(binary, "sandbox", "--", "Rscript", "-e", script)]


def test_cannot_open_a_preexisting_pseudo_terminal(binary: Path) -> Transcript:
    master, slave = pty.openpty()
    slave_name = os.ttyname(slave)
    # fmt: python
    script = code(r"""
        import errno
        import os
        import sys

        for flags in (os.O_RDONLY, os.O_WRONLY):
            try:
                descriptor = os.open(sys.argv[1], flags | os.O_NOCTTY)
            except OSError as error:
                assert error.errno == errno.EPERM
            else:
                os.close(descriptor)
                raise SystemExit("pre-existing pseudo-terminal was accessible")

        print("blocked")
        """)
    try:
        entry = record(
            binary,
            "sandbox",
            "--",
            "python",
            "-c",
            script,
            slave_name,
        )
    finally:
        os.close(master)
        os.close(slave)

    assert "exit_code" not in entry, entry
    assert entry["stdout"] == "blocked\n", entry
    command = entry["command"]
    assert isinstance(command, list)
    command[-1] = "<pre-existing pseudo-terminal>"
    entry["transcript_normalization"] = {
        "target": "command[-1]",
        "pseudo_terminal_path": "omitted",
    }
    return [entry]


def test_cannot_hard_link_a_host_file_into_the_writable_directory(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        host_file = root / "host.txt"
        host_file.write_text("host data", encoding="utf-8")
        # fmt: python
        script = code(r"""
            import errno
            import os
            import pathlib
            import sys

            destination = pathlib.Path(os.environ["TMPDIR"]) / "host-link"
            assert os.stat(sys.argv[1]).st_dev == os.stat(destination.parent).st_dev
            try:
                os.link(sys.argv[1], destination)
            except OSError as error:
                assert error.errno == errno.EPERM
            else:
                destination.write_text("escaped")
                raise SystemExit("host hard-link escape succeeded")

            print("blocked")
            """)
        entry = record(
            binary,
            "sandbox",
            "--",
            "python",
            "-c",
            script,
            "host.txt",
            environment={"TMPDIR": "."},
            current_directory=root,
        )

        assert "exit_code" not in entry, entry
        assert entry["stdout"] == "blocked\n", entry
        assert host_file.read_text(encoding="utf-8") == "host data"
        return [entry]


def test_denies_network_access(binary: Path) -> Transcript:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        # fmt: python
        script = code(r"""
            import errno
            import socket
            import sys

            try:
                socket.create_connection(("127.0.0.1", int(sys.argv[1])))
            except OSError as error:
                assert error.errno == errno.EPERM
                print("blocked")
            else:
                raise SystemExit("network access was allowed")
            """)
        entry = record(
            binary,
            "sandbox",
            "--",
            "python",
            "-c",
            script,
            str(port),
        )
        listener.setblocking(False)
        try:
            listener.accept()
        except BlockingIOError:
            pass
        else:
            raise AssertionError("sandboxed child reached the host listener")

    assert "exit_code" not in entry, entry
    assert entry["stdout"] == "blocked\n", entry
    command = entry["command"]
    assert isinstance(command, list)
    command[-1] = "<listener port>"
    entry["transcript_normalization"] = {
        "target": "command[-1]",
        "listener_port": "omitted",
    }
    return [entry]


if __name__ == "__main__":
    run_this_suite(__file__)
