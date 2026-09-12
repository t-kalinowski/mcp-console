#!/usr/bin/env -S uv run --script

import json
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
from support.native import LOADER_VARIABLE, build_interposer
from support.processes import capture_process_identity, host_process_id, live_processes
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, SANDBOX, WORKER, requires
from support.ssh import SSH, configure, localhost, remote_command
from support.suites import run_this_suite


@requires(SSH, WORKER, NATIVE_FIXTURES)
def test_completed_retirement_precedes_a_due_heartbeat(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        local, remote = root / "local", root / "remote"
        local.mkdir()
        remote.mkdir()
        environment, _ = r_test_environment()
        record = remote / "clock-advanced"
        prefix = remote_command(
            remote,
            binary,
            {
                "R_HOME": environment["R_HOME"],
                "PATH": "/usr/bin:/bin",
                LOADER_VARIABLE: str(build_interposer(remote, "ssh_lease_clock")),
                "MCP_CONSOLE_TEST_SSH_CLOCK_ROLE": "ssh-owner",
                "MCP_CONSOLE_TEST_SSH_CLOCK_TAG": "7",
                "MCP_CONSOLE_TEST_SSH_CLOCK_ORDINAL": "1",
                "MCP_CONSOLE_TEST_SSH_CLOCK_MS": "2000",
                "MCP_CONSOLE_TEST_SSH_CLOCK_RECORD": str(record),
            },
        )
        config = configure(local, remote, prefix)
        target = json.loads(config.read_text())
        target["target"]["lease_ms"] = 6000
        config.write_text(json.dumps(target))
        with localhost(root / "sshd") as controller:
            with McpClient(
                binary, ("serve", "--no-sandbox"), controller, local
            ) as client:
                client.initialize_and_list_tools()
                # Deschedule the owner after it reads the terminal receipt,
                # until its next heartbeat is due, still within the lease.
                result = client.finish()[3:]
                assert record.read_text() == "1"
                return result + [{"completed_retirement_precedes_heartbeat": True}]


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


@requires(SSH)
def test_malformed_owner_reply_is_terminal_before_setup_deadline(binary: Path):
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        peer = root / "bad-reply.py"
        peer.write_text(
            code("""
            import sys
            sys.stdout.buffer.write(bytes([1]) + (65536).to_bytes(4, 'big'))
            sys.stdout.buffer.flush()
            sys.stdin.buffer.read()
            """)
        )
        configure(root, root, [sys.executable, str(peer)])
        with localhost(root / "sshd") as environment:
            with McpClient(
                binary, ("serve", "--no-sandbox"), environment, root
            ) as client:
                client.process.wait(timeout=12)
                client.stdin.close()
                assert not client.stdout.read(timeout=12)
                errors = client.stderr.read(timeout=12)
                assert "SSH frame exceeds 32768 bytes" in errors, errors
                assert "unconfirmed" in errors, errors
                return [{"stderr": errors}]


if __name__ == "__main__":
    run_this_suite(__file__)
