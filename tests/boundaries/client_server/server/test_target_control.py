#!/usr/bin/env -S uv run --script
"""Target setup controls retain their cause and scoped retirement evidence."""

import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.docker_sandbox import calls, cli_peer, configure, workspace
from support.requirements import POSIX, requires
from support.suites import run_this_suite

TEMPLATE = "docker.io/example/console@sha256:" + "a" * 64



@requires(POSIX)
def test_interrupt_during_target_capture_preserves_reason(binary: Path) -> list:
    with workspace() as root:
        environment = cli_peer(root / "peer")
        configure(root, template=TEMPLATE)
        (root / "peer/mode").write_text("diagnostics-gate")
        with closing(FifoCheckpoint.create(root / "peer/reached")) as reached:
            with McpClient(binary, ("serve",), environment, root) as client:
                client.initialize_and_list_tools()
                reached.wait("version command admitted and diagnostics drained")
                client.send(r="must_not_run <- TRUE", timeout_ms=0)
                client.send(control="interrupt", timeout_ms=10000)
                text = last_result_text(client)
                assert "Docker Sandbox setup interrupted" in text, text
                assert "setup cancelled" not in text, text
                _, stderr = client.finish_with_standard_error(expected_exit_status=1)
                assert "Docker Sandbox setup interrupted" in stderr, stderr
        assert [call["args"] for call in calls(root)] == [["version"]]
        return [{"requested_control": "interrupt", "next_setup_command": False}]



@requires(POSIX)
def test_eof_before_provider_creation_confirms_local_retirement(binary: Path) -> list:
    with workspace() as root:
        environment = cli_peer(root / "peer")
        configure(root, template=TEMPLATE)
        (root / "peer/mode").write_text("diagnostics-gate")
        with closing(FifoCheckpoint.create(root / "peer/reached")) as reached:
            with McpClient(binary, ("serve",), environment, root) as client:
                client.initialize_and_list_tools()
                reached.wait("version command admitted and diagnostics drained")
                _, stderr = client.finish_with_standard_error()
                assert stderr == "", stderr
        assert [call["args"] for call in calls(root)] == [["version"]]
        assert not (root / "peer/vms").exists()
        (session,) = (root / ".agents/console/sessions").iterdir()
        assert (
            session / "outputs/session.log"
        ).read_text() == "provider startup\n" * 20000
        return [{"requested_control": "shutdown", "local_retirement_confirmed": True}]



if __name__ == "__main__":
    run_this_suite(__file__)
