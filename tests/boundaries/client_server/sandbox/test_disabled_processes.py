#!/usr/bin/env -S uv run --script

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.normalization import code
from support.records import Transcript, TranscriptWithCompanions
from support.suites import run_this_suite

PLATFORMS = {"darwin", "linux"}


def test_psutil_sees_host_processes_without_sandbox(
    binary: Path,
) -> Transcript | TranscriptWithCompanions:
    with subprocess.Popen(
        ["/bin/cat"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        start_new_session=True,
    ) as sentinel:
        environment = os.environ.copy()
        environment.pop("RETICULATE_PYTHON", None)
        environment["MCP_CONSOLE_TEST_HOST_PROCESS"] = str(sentinel.pid)
        try:
            with McpClient(binary, ("serve", "--no-sandbox"), environment) as client:
                client.initialize_and_list_tools()
                client.send(python="import os")
                assert last_tool_text(client) == "[done]"
                python = code("""
                    import os

                    import psutil

                    host_process = int(os.environ["MCP_CONSOLE_TEST_HOST_PROCESS"])
                    assert os.getpgid(host_process) != os.getpgrp()
                    print(
                        host_process in psutil.pids(),
                        host_process in {process.pid for process in psutil.process_iter()},
                    )
                    """)
                # Exercise both live installation and startup with retained psutil.
                client.send(python=python)
                assert last_tool_text(client) == "True True\n", client.transcript[-1]
                client.send(control="restart", python=python)
                assert last_tool_text(client) == (
                    "[worker stopped: in-memory state lost]\n"
                    "[starting new worker]\nTrue True\n[done]"
                ), client.transcript[-1]
                transcript = client.finish()
                if sys.platform == "linux":
                    return TranscriptWithCompanions(transcript, {}, platform="linux")
                return transcript
        finally:
            assert sentinel.stdin is not None
            sentinel.stdin.close()
            sentinel.wait(timeout=5)


if __name__ == "__main__":
    run_this_suite(__file__)
