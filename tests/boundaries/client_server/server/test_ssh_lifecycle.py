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
from support.records import Transcript
from support.requirements import SANDBOX, WORKER, requires
from support.ssh import SSH, configure, localhost
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
            printf '%s\n' "$PPID" "$TMPDIR" > STATE
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
            [str(binary)],
            extends=":workspace",
            sandbox={
                "inherit_environment": False,
                "environment": {"PATH": str(remote)},
            },
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
                    client.initialize_and_list_tools()
                    client.start_send(r="must_not_run <- TRUE")
                    checkpoint.wait("remote worker is gated before readiness")
                    pid, private = state.read_text().splitlines()
                    client.stdin.close()
                    output = client.stdout.read(timeout=12)
                    errors = client.stderr.read(timeout=12)
                    client.process.wait(timeout=12)
                    assert client.process.returncode == 0, (output, errors)
                    assert not errors, errors
                    assert not Path(private).exists(), private
                    try:
                        os.kill(int(pid), 0)
                    except ProcessLookupError:
                        pass
                    else:
                        raise AssertionError(
                            "remote worker survived startup cancellation"
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
                client.initialize_and_list_tools()
                client.send(r="must_not_run <- TRUE")
                result = last_result_text(client)
                assert "unconfirmed" in result, result
                client.stdin.close()
                client.stdout.read(timeout=12)
                errors = client.stderr.read(timeout=12)
                client.process.wait(timeout=12)
                assert "/console-test-unavailable" in errors, errors
                # Shell diagnostic spelling varies with the remote account's
                # configured shell. The original diagnostic must reach stderr.
                return client.transcript[3:] + [
                    {"remote_shell_diagnostic_retained": True}
                ]


if __name__ == "__main__":
    run_this_suite(__file__)
