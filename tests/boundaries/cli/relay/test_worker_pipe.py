#!/usr/bin/env -S uv run --script

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.capture import read_lines
from support.records import Transcript
from support.requirements import WORKER, requires
from support.suites import run_this_suite


@requires(WORKER)
def test_worker_reports_closed_output_pipe_without_r_sigpipe_handler(
    binary: Path,
) -> Transcript:
    worker_read, relay_write = os.pipe()
    relay_read, worker_write = os.pipe()
    environment = os.environ.copy()
    environment["MCP_CONSOLE_SIDEBAND_READ_FD"] = str(worker_read)
    environment["MCP_CONSOLE_SIDEBAND_WRITE_FD"] = str(worker_write)
    process = subprocess.Popen(
        [binary, "worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(worker_read, worker_write),
        env=environment,
        text=True,
    )
    os.close(worker_read)
    os.close(worker_write)
    with os.fdopen(relay_read) as output, os.fdopen(relay_write, "w") as commands:
        try:
            assert json.loads(read_lines(output, 1, "worker ready")[0]) == {
                "kind": "ready"
            }
            output.close()
            commands.write(
                json.dumps(
                    {
                        "kind": "evaluate",
                        "language": "r",
                        "source": "cat('closed pipe\\n')",
                    }
                )
                + "\n"
            )
            commands.flush()
            # Keep stdin and the command pipe open until failure is observed.
            # Their EOF must not cause ordinary worker retirement first.
            assert process.wait(timeout=5) == 1
            stdout, stderr = process.communicate(timeout=5)
            assert stdout == "", stdout
            assert stderr == "R console output failed: Broken pipe (os error 32)\n", (
                stderr
            )
            return [
                {"exit_code": process.returncode, "stdout": stdout, "stderr": stderr}
            ]
        finally:
            if process.returncode is None:
                process.kill()
            process.communicate(timeout=5)


if __name__ == "__main__":
    run_this_suite(__file__)
