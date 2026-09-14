#!/usr/bin/env -S uv run --script
"""Public acceptance against real execution providers without an R runtime."""

import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.client import McpClient
from support import docker, docker_sandbox, ssh
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.requirements import NO_R, Requirement, requires
from support.suites import run_this_suite


PYTHON_ONLY_TARGETS = Requirement(
    "prepared Python-only targets",
    os.environ.get("MCP_CONSOLE_TEST_PYTHON_ONLY_TARGETS") == "1",
    "requires explicitly selected Docker/SBX fixtures built without R",
)


def exercise_no_r_catalog(client: McpClient, *, managed: bool) -> None:
    client.initialize_and_list_tools()
    properties = client.transcript[-1]["result"]["tools"][0]["inputSchema"][
        "properties"
    ]
    assert ("requirements" in properties) == managed
    client.send(sql="CREATE TABLE answers AS SELECT 42 AS answer")
    # fmt: python
    python = code("""
        import ctypes
        import os
        import shutil
        from pathlib import Path

        assert shutil.which("R") is None and shutil.which("Rscript") is None
        assert not hasattr(ctypes.CDLL(None), "Rf_initialize_R")
        original_pid = os.getpid()
        answer = 41
        assert sql_connection().execute("SELECT answer FROM answers").fetchone() == (42,)
        answer + 1
        """)
    client.send(python=python)
    assert last_result_text(client) == "42\n", last_result_text(client)
    if managed:
        client.send(requirements={"python": ["py-yaml12"]})
        assert last_result_text(client) == "[prepared]"
    client.send(r="1 + 1")
    assert "R is unavailable" in last_result_text(client)
    client.send(python="assert os.getpid() == original_pid\nanswer + 1")
    assert last_result_text(client) == "42\n"
    client.send(sql="SELECT answer FROM answers")
    assert "42" in last_result_text(client)
    client.send(python="input('target> ')")
    assert "[waiting for stdin]" in last_result_text(client)
    client.send(stdin="exact input\n")
    assert last_result_text(client) == "'exact input'\n"


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
                client.send(python="import yaml12\n'answer' in globals()")
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


@requires(docker.DOCKER, PYTHON_ONLY_TARGETS)
def test_prepared_no_r_docker_image(binary: Path) -> list:
    with docker.workspace() as root:
        reference = docker.image()
        docker.configure(root, reference)
        environment = {
            **os.environ,
            "R_HOME": "/controller-r-must-not-be-used",
            "RETICULATE_PYTHON": "/controller-python-must-not-be-used",
        }
        with McpClient(binary, ("serve", "--no-sandbox"), environment, root) as client:
            exercise_no_r_catalog(client, managed=False)
            client.send(python="print(Path('/etc/hostname').read_text().strip())")
            first = last_result_text(client).strip()
            client.transcript[-1]["result"]["content"][0]["text"] = (
                "<first container>\n"
            )
            client.send(control="restart")
            docker.absent(first)
            client.send(
                python="from pathlib import Path\nprint(Path('/etc/hostname').read_text().strip())"
            )
            second = last_result_text(client).strip()
            assert second != first
            client.transcript[-1]["result"]["content"][0]["text"] = (
                "<second container>\n"
            )
            transcript = client.finish()
        docker.absent(second)
        return json.loads(json.dumps(transcript).replace(reference, "<image>"))


@requires(docker_sandbox.DOCKER_SANDBOX, PYTHON_ONLY_TARGETS)
def test_prepared_no_r_sbx_template(binary: Path) -> list:
    with docker_sandbox.workspace(real=True) as root:
        docker_sandbox.configure(root)
        environment = {
            **os.environ,
            "R_HOME": "/controller-r-must-not-be-used",
            "RETICULATE_PYTHON": "/controller-python-must-not-be-used",
        }
        with McpClient(binary, ("serve",), environment, root) as client:
            exercise_no_r_catalog(client, managed=False)
            client.send(control="restart")
            client.send(python="'answer' in globals()")
            assert last_result_text(client) == "False\n"
            transcript = docker_sandbox.finish(client, root)
        generations = docker_sandbox.generations(root)
        assert len(generations) == 2
        for generation in generations:
            docker_sandbox.absent(generation["name"], generation["id"])
        return json.loads(
            json.dumps(transcript).replace(
                os.environ["MCP_CONSOLE_TEST_SBX_TEMPLATE"], "<template>"
            )
        )


if __name__ == "__main__":
    run_this_suite(__file__)
