#!/usr/bin/env -S uv run --script
"""Public acceptance for R-free hosts over real OpenSSH."""

import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.client import McpClient
from support import ssh
from support.execution import DIRECT, SANDBOXED, Execution
from support.no_r import exercise_no_r_catalog
from support.normalization import code
from support.requirements import NO_R, requires
from support.suites import run_this_suite


def managed_no_r_host_over_openssh(binary: Path, execution: Execution) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        controller, remote = root / "controller", root / "remote"
        controller.mkdir()
        remote.mkdir()
        ssh.configure(controller, remote, [str(binary)])
        with ssh.localhost(
            root / "sshd", remote_path=os.environ["PATH"]
        ) as environment:
            environment.update(
                R_HOME="/controller-r-must-not-be-used",
                RETICULATE_PYTHON="/controller-python-must-not-be-used",
            )
            with McpClient(
                binary, execution.serve(), environment, controller
            ) as client:
                exercise_no_r_catalog(client, managed=True)
                client.send(control="restart")
                client.send(
                    # fmt: python
                    python=code("""
                        import yaml12

                        "answer" in globals()
                        """)
                )
                assert last_result_text(client) == "False\n"
                return json.loads(
                    json.dumps(client.finish()).replace(str(root), "<ssh-test>")
                )


@requires(NO_R, ssh.SSH)
def test_managed_no_r_host_over_openssh(binary: Path) -> list:
    return managed_no_r_host_over_openssh(binary, DIRECT)


@requires(NO_R, ssh.SSH)
def test_managed_no_r_host_over_openssh_with_sandbox(binary: Path) -> list:
    return managed_no_r_host_over_openssh(binary, SANDBOXED)


if __name__ == "__main__":
    run_this_suite(__file__)
