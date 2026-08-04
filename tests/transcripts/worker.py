#!/usr/bin/env -S uv run --script

from pathlib import Path

from _support import Transcript, WorkerClient, code, run_this_suite


PLATFORMS = {"darwin"}


def test_routes_python_output(binary: Path) -> Transcript:
    client = WorkerClient(binary)
    # fmt: r
    r = code(r"""
        python <- Sys.which("python3")
        stopifnot(nzchar(python))
        reticulate::use_python(python, required = TRUE)
        suppressWarnings(
          invisible(reticulate::py_run_string("initialized_from_r = True"))
        )
        """)
    client.evaluate("r", r)

    # fmt: python
    python = code(r"""
        import sys

        assert initialized_from_r
        print("Python stdout")
        sys.stderr.write("Python stderr\n")
        raise ValueError("boom")
        """)
    client.evaluate("python", python)

    # fmt: python
    descendant = code(r"""
        import os

        os.write(1, b"descendant stdout\n")
        os.write(2, b"descendant stderr\n")
        """)
    # fmt: python
    python = code(rf"""
        import os
        import subprocess
        import sys

        buffer_stdout = sys.stdout.buffer.write(b"buffer stdout\n")
        sys.stdout.buffer.flush()
        buffer_stderr = sys.stderr.buffer.write(b"buffer stderr\n")
        sys.stderr.buffer.flush()
        direct_stdout = os.write(1, b"direct stdout\n")
        direct_stderr = os.write(2, b"direct stderr\n")
        descendant_source = {descendant!r}
        descendant = subprocess.run(
            [sys.executable, "-c", descendant_source],
            check=True,
        )
        """)
    client.evaluate("python", python)
    transcript = client.finish()
    assert transcript[-5:] == [
        {"stdout": "buffer stdout\ndirect stdout\ndescendant stdout\n"},
        {"stderr": "buffer stderr\ndirect stderr\ndescendant stderr\n"},
        {"worker": {"kind": "completed"}},
        {"server": {"kind": "shutdown"}},
        {"exit_code": 0},
    ], transcript[-5:]
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
