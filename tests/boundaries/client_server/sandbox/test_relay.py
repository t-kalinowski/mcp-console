#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient, stop_client
from support.macos import (
    capture_darwin_process_identity,
    kill_darwin_processes,
    live_darwin_processes,
)
from support.normalization import code
from support.records import Transcript
from support.suites import run_this_suite

PLATFORMS = {"darwin"}


def test_restart_and_shutdown_with_relay_below_sandbox_root(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        wrapper = Path(directory) / "relay-wrapper"
        # A process wrapper may retain its own identity while launching the
        # relay as an ordinary child. Only standard streams carry relay data.
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            + code(r"""
                import os
                import subprocess
                import sys

                relay = subprocess.Popen([
                    os.environ["MCP_CONSOLE_TEST_BINARY"], "worker-relay", *sys.argv[1:]
                ])
                sys.exit(relay.wait())
                """),
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        worker = Path(directory) / "worker-wrapper"
        worker.write_text(
            "#!/usr/bin/env python3\n"
            + code(r"""
                import os

                binary = os.environ["MCP_CONSOLE_TEST_BINARY"]
                os.execv(binary, [binary, "worker"])
                """),
            encoding="utf-8",
        )
        worker.chmod(0o755)
        environment = os.environ.copy()
        environment["MCP_CONSOLE_TEST_BINARY"] = str(binary)
        client = McpClient(
            binary,
            ("serve", "--worker", str(worker), "--relay", str(wrapper)),
            environment,
        )
        identities = []
        try:
            client._initialize_and_list_tools()
            for retirement in ("restart", "shutdown"):
                client.send(
                    python=code(r"""
                    import os
                    import subprocess

                    child = subprocess.Popen(["/bin/sleep", "60"])
                    print(os.getpgrp(), os.getppid(), os.getpid(), child.pid)
                    print(os.environ["TMPDIR"])
                    """)
                )
                processes, temporary_directory = last_tool_text(client).splitlines()
                root, relay, worker_pid, descendant = map(int, processes.split())
                assert root != relay, "relay unexpectedly replaced the sandbox root"
                assert os.getpgid(relay) == root
                identities.extend(
                    capture_darwin_process_identity(pid)
                    for pid in (root, relay, worker_pid, descendant)
                )
                client.transcript[-1]["result"]["content"][0]["text"] = (
                    "<sandbox root> <relay pid> <worker pid> <descendant pid>\n<sandbox temp>\n"
                )
                client.transcript[-1]["transcript_normalization"] = {
                    "target": "result.content[0].text",
                    "process_ids": "omitted",
                    "sandbox_temporary_directory": "omitted",
                }

                if retirement == "restart":
                    client.send(control="restart", python='print("replacement ready")')
                    assert last_tool_text(client) == (
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\nreplacement ready\n[done]"
                    ), client.transcript[-1]
                else:
                    client._finish()
                assert live_darwin_processes(tuple(identities)) == []
                assert not Path(temporary_directory).exists()
            return client.transcript
        finally:
            stop_client(client)
            kill_darwin_processes(tuple(identities))


if __name__ == "__main__":
    run_this_suite(__file__)
