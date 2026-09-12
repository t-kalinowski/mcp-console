#!/usr/bin/env -S uv run --script

import os
import shlex
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.checkpoints import FifoCheckpoint
from support.assertions import last_result_text
from support.client import McpClient
from support.normalization import code
from support.processes import capture_process_identity, host_process_id, live_processes
from support.records import Transcript
from support.requirements import SANDBOX, WORKER, requires
from support.ssh import SSH, configure, localhost, remote_command
from support.suites import run_this_suite


@requires(SSH, WORKER, SANDBOX)
def test_startup_cancellation_preserves_shared_connection(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        local, remote = root / "local", root / "remote"
        local.mkdir()
        remote.mkdir()
        checkpoint = FifoCheckpoint.create(remote / "reached")
        gate = remote / "hold"
        os.mkfifo(gate)
        state = remote / "state"
        r = remote / "R"
        r.write_text(
            code(r"""
            #!/bin/sh
            printf '%s\n' "$$" > STATE
            printf 1 > CHECKPOINT
            exec /bin/cat GATE
            """)
            .replace("STATE", shlex.quote(str(state)))
            .replace("CHECKPOINT", shlex.quote(str(checkpoint.path)))
            .replace("GATE", shlex.quote(str(gate)))
        )
        r.chmod(0o755)
        configure(
            local,
            remote,
            remote_command(remote, binary, {"PATH": str(remote)}),
        )
        with localhost(root / "sshd") as environment:
            ssh = root / "sshd/ssh"
            subprocess.run(
                [ssh, "-MNf", "-o", "ControlMaster=yes", "console-test"],
                check=True,
                capture_output=True,
                timeout=10,
            )
            try:
                with McpClient(binary, ("serve",), environment, local) as client:
                    checkpoint.wait("remote R probe is gated before MCP readiness")
                    pid = state.read_text().strip()
                    # The test owns sshd; its remote worker is not a descendant
                    # of the local MCP client or the shared SSH connection.
                    worker = capture_process_identity(
                        host_process_id(int(pid), os.getpid())
                    )
                    client.stdin.close()
                    output = client.stdout.read(timeout=12)
                    errors = client.stderr.read(timeout=12)
                    client.process.wait(timeout=12)
                    assert client.process.returncode != 0, (output, errors)
                    assert "closed" in errors, errors
                    assert not live_processes([worker]), (
                        "remote R probe survived startup cancellation"
                    )
                subprocess.run(
                    [ssh, "-O", "check", "console-test"],
                    check=True,
                    capture_output=True,
                    timeout=10,
                )
                return [
                    {
                        "startup_cancelled": True,
                        "remote_retirement_confirmed": True,
                        "shared_connection_survived": True,
                    }
                ]
            finally:
                # Only this fixture owns the master and may stop it.
                subprocess.run(
                    [ssh, "-O", "exit", "console-test"], capture_output=True, timeout=10
                )
                checkpoint.close()


@requires(SSH)
def test_unavailable_remote_command(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        configure(root, root, ["/console-test-unavailable"])
        with localhost(root / "sshd") as environment:
            with McpClient(
                binary, ("serve", "--no-sandbox"), environment, root
            ) as client:
                client.process.wait(timeout=12)
                client.stdin.close()
                client.stdout.read(timeout=12)
                errors = client.stderr.read(timeout=12)
                client.process.wait(timeout=12)
                assert "/console-test-unavailable" in errors, errors
                # Shell diagnostic spelling varies with the remote account's
                # configured shell. The original diagnostic must reach stderr.
                assert "SSH preparation retirement is unconfirmed" in errors, errors
                return [{"mcp_ready": False, "remote_shell_diagnostic_retained": True}]


if __name__ == "__main__":
    run_this_suite(__file__)
