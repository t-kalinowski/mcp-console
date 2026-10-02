#!/usr/bin/env -S uv run --script
"""Public acceptance for R-free hosts over real OpenSSH."""

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.client import McpClient
from support import ssh
from boundaries.client_server.server.test_no_r import no_r_environment
from support.execution import DIRECT, SANDBOXED, Execution
from support.no_r import exercise_no_r_catalog
from support.normalization import code
from support.requirements import SANDBOX, command, requires
from support.suites import run_this_suite


def managed_no_r_host_over_openssh(binary: Path, execution: Execution) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        controller, remote = root / "controller", root / "remote"
        controller.mkdir()
        remote.mkdir()
        remote_environment = no_r_environment(remote)
        prefix = ssh.remote_command(
            remote,
            binary,
            {"PATH": remote_environment["PATH"], "HOME": str(remote)},
        )
        ssh.configure(controller, remote, prefix)
        with ssh.localhost(root / "sshd") as environment:
            trap = ssh.poison_controller(root / "sshd", environment)
            with McpClient(
                binary, execution.serve(), environment, controller
            ) as client:
                exercise_no_r_catalog(client)
                client.send(control="restart")
                client.send(
                    # fmt: python
                    python=code("""
                        import yaml12

                        "answer" in globals()
                        """)
                )
                assert last_result_text(client) == "False\n"
                transcript = client.finish()
                assert not trap.exists(), "controller attempted runtime discovery"
                return json.loads(
                    json.dumps(transcript).replace(str(root), "<ssh-test>")
                )


@requires(ssh.SSH, command("uv"))
def test_managed_no_r_host_over_openssh(binary: Path) -> list:
    return managed_no_r_host_over_openssh(binary, DIRECT)


@requires(ssh.SSH, command("uv"), SANDBOX)
def test_managed_no_r_host_over_openssh_with_sandbox(binary: Path) -> list:
    return managed_no_r_host_over_openssh(binary, SANDBOXED)


if __name__ == "__main__":
    run_this_suite(__file__)
