#!/usr/bin/env -S uv run --script

import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent

from _support import Transcript, TranscriptEntry, run_this_suite


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


def test_preserves_python_arguments_and_standard_output(binary: Path) -> Transcript:
    # fmt: python
    script = dedent(r"""
        import sys

        print("|".join(sys.argv[1:]))
        """).strip("\n")
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
    script = dedent(r"""
        import sys

        for line in sys.stdin:
            if line == "EXIT\n":
                break

            sys.stdout.write(line)
            sys.stdout.flush()
            sys.stderr.write(line)
            sys.stderr.flush()
        """).strip("\n")
    arguments = ("sandbox", "--", "python", "-c", script)

    with subprocess.Popen(
        [binary, *arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    ) as process:
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None

        input_line = "echo exactly: $(literal)\n"
        process.stdin.write(input_line)
        process.stdin.flush()
        echoed_output = process.stdout.readline()
        echoed_error = process.stderr.readline()

        assert process.poll() is None
        assert echoed_output == input_line
        assert echoed_error == input_line

        process.stdin.write("EXIT\n")
        process.stdin.flush()
        exit_code = process.wait(timeout=5)
        process.stdin.close()
        assert process.stdout.read() == ""
        assert process.stderr.read() == ""
        assert exit_code == 0

    return [
        {
            "command": ["mcp-console", *arguments],
            "stdin": input_line,
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
    script = dedent(r"""
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
        """).strip("\n")
    return [record(binary, "sandbox", "--", "python", "-c", script)]


def test_does_not_require_home(binary: Path) -> Transcript:
    # fmt: python
    script = dedent(r"""
        print("ran")
        """).strip("\n")
    arguments = ("sandbox", "--", "python", "-c", script)
    return [record(binary, *arguments, environment={"HOME": None})]


def test_supports_r_runtime_queries_and_temporary_writes(binary: Path) -> Transcript:
    # fmt: r
    script = dedent(r"""
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
        """).strip("\n")
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
    script = dedent(r"""
        {
          p <- processx::process$new("/bin/cat", pty = TRUE)
          on.exit(if (p$is_alive()) p$kill())
          p$write_input("sandboxed pty\n")
          stopifnot(p$poll_io(5000)[["output"]] == "ready")
          cat(p$read_output())
          invisible(p$kill())
        }
        """).strip("\n")
    return [record(binary, "sandbox", "--", "Rscript", "-e", script)]


if __name__ == "__main__":
    run_this_suite(__file__)
