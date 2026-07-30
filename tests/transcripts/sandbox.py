#!/usr/bin/env -S uv run --script

import os
import subprocess
from pathlib import Path
from textwrap import dedent

from _support import Transcript, run_this_suite


PLATFORMS = {"darwin"}


def record(
    binary: Path,
    *arguments: str,
    environment: dict[str, str | None] | None = None,
) -> dict[str, object]:
    child_environment = os.environ.copy()
    for name, value in (environment or {}).items():
        if value is None:
            child_environment.pop(name, None)
        else:
            child_environment[name] = value

    result = subprocess.run(
        [binary, *arguments],
        capture_output=True,
        env=child_environment,
    )
    transcript: dict[str, object] = {
        "command": ["mcp-console", *arguments],
    }
    if environment is not None:
        transcript["environment"] = environment
    if result.returncode != 0:
        transcript["exit_code"] = result.returncode
    transcript["stdout"] = result.stdout.decode("utf-8")
    if result.stderr:
        transcript["stderr"] = result.stderr.decode("utf-8")
    return transcript


def test_preserves_python_arguments_and_standard_output(binary: Path) -> Transcript:
    # fmt: python
    script = dedent(
        r"""
        import sys

        print("|".join(sys.argv[1:]))
        """
    ).strip()
    return [
        record(
            binary,
            "sandbox",
            "python",
            "-c",
            script,
            "hello world",
            "$(not-a-command)",
            "--child-option",
        )
    ]


def test_allows_python_multiprocessing_semaphores(binary: Path) -> Transcript:
    # fmt: python
    script = dedent(
        r"""
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
        """
    ).strip()
    return [
        record(
            binary,
            "sandbox",
            "--",
            "python",
            "-c",
            script,
        )
    ]


def test_does_not_require_home(binary: Path) -> Transcript:
    # fmt: python
    script = dedent(
        r"""
        print("ran")
        """
    ).strip()
    return [
        record(
            binary,
            "sandbox",
            "--",
            "python",
            "-c",
            script,
            environment={"HOME": None},
        )
    ]


def test_supports_r_runtime_queries_and_temporary_writes(binary: Path) -> Transcript:
    # fmt: r
    script = dedent(
        r"""
        {
          stopifnot(parallel::detectCores() >= 1)

          output <- file.path(tempdir(), "result.txt")
          writeLines("sandboxed R", output)
          writeLines(readLines(output))
        }
        """
    ).strip()
    return [
        record(
            binary,
            "sandbox",
            "--",
            "Rscript",
            "-e",
            script,
        )
    ]


def test_allows_processx_pty_processes(binary: Path) -> Transcript:
    # fmt: r
    script = dedent(
        r"""
        {
          p <- processx::process$new("/bin/cat", pty = TRUE)
          on.exit(if (p$is_alive()) p$kill())
          p$write_input("sandboxed pty\n")
          stopifnot(p$poll_io(5000)[["output"]] == "ready")
          cat(p$read_output())
          invisible(p$kill())
        }
        """
    ).strip()
    return [
        record(
            binary,
            "sandbox",
            "--",
            "Rscript",
            "-e",
            script,
        )
    ]


if __name__ == "__main__":
    run_this_suite(__file__)
