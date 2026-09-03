#!/usr/bin/env -S uv run --script

import os
import selectors
import shutil
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


def test_preserves_executable_names_with_equals_signs(binary: Path) -> Transcript:
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
    return [entry]


def test_preserves_executable_names_that_look_like_options(binary: Path) -> Transcript:
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
    return [entry]


def test_preserves_python_arguments_and_standard_output(binary: Path) -> Transcript:
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
    return [record(binary, *arguments)]


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


def test_allows_python_multiprocessing_semaphores(binary: Path) -> Transcript:
    # fmt: python
    script = code(r"""
        import multiprocessing as mp
        import operator

        context = mp.get_context("spawn")
        lock = context.Lock()
        lock.acquire()
        child = context.Process(target=operator.methodcaller("release"), args=(lock,))
        child.start()
        child.join()
        assert child.exitcode == 0
        assert lock.acquire(timeout=1)
        print("semaphore shared")
        """)
    return [record(binary, "sandbox", "--", "python", "-c", script)]


def test_does_not_require_home(binary: Path) -> Transcript:
    # fmt: python
    script = code(r"""
        print("ran")
        """)
    arguments = ("sandbox", "--", "python", "-c", script)
    return [record(binary, *arguments, environment={"HOME": None})]


def test_supports_r_runtime_queries_and_temporary_writes(binary: Path) -> Transcript:
    # fmt: r
    script = code(r"""
        {
          stopifnot(parallel::detectCores() >= 1)
          stopifnot(file.exists("Cargo.toml"))

          host_write <- try(
            suppressWarnings(file("Cargo.toml", open = "r+")),
            silent = TRUE
          )
          stopifnot(inherits(host_write, "try-error"))

          output <- file.path(tempdir(), "result.txt")
          writeLines("sandboxed R", output)
          writeLines(readLines(output))
          writeLines(Sys.getenv("TMPDIR"))
        }
        """)
    entry = record(binary, "sandbox", "--", "Rscript", "-e", script)
    stdout = entry["stdout"]
    assert isinstance(stdout, str)
    output, temporary_directory = stdout.splitlines()
    assert output == "sandboxed R"
    assert not Path(temporary_directory).exists()
    entry["stdout"] = f"{output}\n"
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


if __name__ == "__main__":
    run_this_suite(__file__)
