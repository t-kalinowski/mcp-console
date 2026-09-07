#!/usr/bin/env -S uv run --script

import os
import signal
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.events import Events
from support.client import McpClient, stop_client
from support.processes import (
    capture_process_identity,
    child_process_identities,
    kill_processes,
    live_processes,
    signal_process,
)
from support.records import Transcript, TranscriptWithCompanions
from support.suites import run_this_suite

PLATFORMS = {"darwin", "linux"}


def test_replaces_direct_relay_after_sigkill(
    binary: Path,
) -> Transcript | TranscriptWithCompanions:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as directory, Events() as events:
        temporary = Path(directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = directory
        client = McpClient(
            binary,
            ("serve", "--no-sandbox", "--worker", str(zod)),
            environment,
        )
        identities = []
        try:
            client.initialize_and_list_tools()
            client.send(r="echo ready")
            assert last_tool_text(client) == "zod: ready\n"
            server = capture_process_identity(client.process.pid)
            (relay,) = child_process_identities(server)
            (worker,) = child_process_identities(relay)
            identities.extend((relay, worker))

            events.watch_file(temporary)
            client.send(r="stall", timeout_ms=0)
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            deadline = time.monotonic() + 10
            while not (temporary / "zod-stalled").exists():
                remaining = deadline - time.monotonic()
                assert remaining > 0, "worker did not enter its stalled evaluation"
                assert events.wait(remaining), "worker did not stall"

            signal_process(relay, signal.SIGKILL)
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
            assert live_processes((relay,)) == []
            (replacement,) = child_process_identities(server)
            assert replacement != relay
            identities.append(replacement)
            identities.extend(child_process_identities(replacement))
            client.send(r="echo recovered")
            assert last_tool_text(client) == "zod: recovered\n"
            transcript = client.finish()
            if sys.platform == "linux":
                return TranscriptWithCompanions(transcript, {}, platform="linux")
            return transcript
        finally:
            stop_client(client)
            kill_processes(identities)


if __name__ == "__main__":
    run_this_suite(__file__)
