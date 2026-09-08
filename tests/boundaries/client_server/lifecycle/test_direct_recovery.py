#!/usr/bin/env -S uv run --script

import os
import select
import signal
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient, stop_client
from support.execution import DIRECT
from support.macos import (
    capture_darwin_process_identity,
    darwin_child_process_identities,
    kill_darwin_processes,
    live_darwin_processes,
    signal_darwin_process,
)
from support.records import Transcript
from support.requirements import PROCESS_EVENTS, WORKER, requires
from support.suites import run_this_suite


@requires(WORKER, PROCESS_EVENTS)
def test_replaces_direct_relay_after_sigkill(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as directory, closing(select.kqueue()) as events:
        temporary = Path(directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = directory
        client = McpClient(
            binary,
            DIRECT.serve("--worker", str(zod)),
            environment,
        )
        identities = []
        descriptor = os.open(temporary, os.O_RDONLY)
        try:
            client.initialize_and_list_tools()
            client.send(r="echo ready")
            assert last_tool_text(client) == "zod: ready\n"
            server = capture_darwin_process_identity(client.process.pid)
            (relay,) = darwin_child_process_identities(server)
            (worker,) = darwin_child_process_identities(relay)
            identities.extend((relay, worker))

            watch = select.kevent(
                descriptor,
                filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                fflags=select.KQ_NOTE_WRITE,
            )
            assert events.control([watch], 0, 0) == []
            client.send(r="stall", timeout_ms=0)
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            deadline = time.monotonic() + 10
            while not (temporary / "zod-stalled").exists():
                remaining = deadline - time.monotonic()
                assert remaining > 0, "worker did not enter its stalled evaluation"
                assert events.control(None, 1, remaining), "worker did not stall"

            signal_darwin_process(relay, signal.SIGKILL)
            result = client.send()
            assert result["isError"] is True, result
            assert result["content"] == [
                {
                    "type": "text",
                    "text": (
                        "[worker relay stdout closed before retirement completed]\n"
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n"
                        "[idle]"
                    ),
                }
            ], result
            assert live_darwin_processes((relay,)) == []
            (replacement,) = darwin_child_process_identities(server)
            assert replacement != relay
            identities.append(replacement)
            identities.extend(darwin_child_process_identities(replacement))
            client.send(r="echo recovered")
            assert last_tool_text(client) == "zod: recovered\n"
            return client.finish()
        finally:
            stop_client(client)
            kill_darwin_processes(identities)
            os.close(descriptor)


if __name__ == "__main__":
    run_this_suite(__file__)
