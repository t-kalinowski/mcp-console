#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient, stop_client
from support.execution import SANDBOXED
from support.macos import (
    capture_darwin_process_identity,
    darwin_child_process_identities,
)
from support.records import Transcript
from support.requirements import (
    MACOS_SANDBOX,
    PROCESS_EVENTS,
    requires,
)
from support.suites import run_this_suite


def _assert_zod_echo(entry: dict[str, object]) -> None:
    result = entry["result"]
    assert result == {
        "content": [{"type": "text", "text": "zod: echo\n"}],
        "isError": False,
    }, result


@requires(MACOS_SANDBOX, PROCESS_EVENTS)
def test_sandbox_setup_failure_is_reported_and_retryable(binary: Path) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as directory:
        temporary_parent = Path(directory) / "sandbox-parent"
        temporary_parent.write_text("not a directory", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = str(temporary_parent)
        client = McpClient(
            binary, SANDBOXED.serve("--worker", str(worker)), environment
        )
        try:
            server = capture_darwin_process_identity(client.process.pid)
            client.initialize_and_list_tools()
            result = client.send(r="echo echo")
            assert result == {
                "content": [
                    {"type": "text", "text": "[worker relay exited before readiness]"}
                ],
                "isError": True,
            }, result
            assert darwin_child_process_identities(server) == ()

            temporary_parent.unlink()
            temporary_parent.mkdir()
            client.send(r="echo echo")
            _assert_zod_echo(client.transcript[-1])
            transcript, stderr = client.finish_with_standard_error()
            assert stderr == (
                "mcp-console-sandbox: create private storage: "
                "Not a directory (os error 20)\n"
            ), stderr
            transcript.append({"stderr": stderr})
            return transcript
        finally:
            stop_client(client)


if __name__ == "__main__":
    run_this_suite(__file__)
