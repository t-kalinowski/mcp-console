#!/usr/bin/env -S uv run --script
"""Public acceptance for prepared Docker and SBX images without R."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support import docker, docker_sandbox
from support.assertions import last_result_text
from support.client import McpClient
from support.no_r import exercise_no_r_catalog
from support.normalization import code
from support.requirements import Requirement, requires
from support.suites import run_this_suite


PYTHON_ONLY_TARGETS = Requirement(
    "prepared Python-only targets",
    os.environ.get("MCP_CONSOLE_TEST_PYTHON_ONLY_TARGETS") == "1",
    "requires explicitly selected Docker/SBX fixtures built without R",
)


def reject_invalid_r_home(client: McpClient) -> list:
    client.start_request(
        "initialize",
        protocolVersion="2025-11-25",
        capabilities={},
        clientInfo={"name": "prepared-r-selection", "version": "1"},
    )
    assert client.stdout.readline(timeout=40) == "", "invalid R_HOME reached readiness"
    error = client.stderr.read(timeout=40)
    assert "R_HOME must select an existing R installation" in error, error
    assert client.process.wait(timeout=5) != 0
    return [{"stderr": error}]


@requires(docker.DOCKER, PYTHON_ONLY_TARGETS)
def test_prepared_docker_rejects_invalid_r_home(binary: Path) -> list:
    with docker.workspace() as root:
        docker.configure(root, docker.image(), environment={"R_HOME": "/missing-r"})
        with McpClient(
            binary, ("serve", "--no-sandbox"), current_directory=root
        ) as client:
            return reject_invalid_r_home(client)


@requires(docker.DOCKER, PYTHON_ONLY_TARGETS)
def test_prepared_docker_rejects_unusable_python_library(binary: Path) -> list:
    records = []
    for name in ("missing", "unloadable"):
        with docker.workspace() as root:
            program = root / "python-library"
            program.write_text(
                # fmt: python
                code(f"""
                    #!/usr/bin/env python3
                    import sys
                    import sysconfig

                    original = sysconfig.get_config_var


                    def selected_library(key):
                        if key == "LIBDIR":
                            return "/selected-library"
                        if key == "INSTSONAME":
                            return "{name}.so"
                        return original(key)


                    sysconfig.get_config_var = selected_library
                    exec(sys.argv[2])
                    """)
            )
            program.chmod(0o755)
            library = root / "unloadable.so"
            library.write_text("not a shared library\n")
            docker.configure(
                root,
                docker.image(),
                environment={"RETICULATE_PYTHON": "/python-library"},
                mounts=[
                    {"source": str(program), "target": "/python-library"},
                    {
                        "source": str(library),
                        "target": "/selected-library/unloadable.so",
                    },
                ],
            )
            with McpClient(
                binary, ("serve", "--no-sandbox"), current_directory=root
            ) as client:
                client.start_request(
                    "initialize",
                    protocolVersion="2025-11-25",
                    capabilities={},
                    clientInfo={"name": "prepared-python-library", "version": "1"},
                )
                response = client.stdout.readline(timeout=40)
                assert response == "", (name, response)
                error = client.stderr.read(timeout=40)
                assert "container Python probe failed" in error, error
                assert f"/selected-library/{name}.so" in error, error
                assert client.process.wait(timeout=5) != 0
                records.append({"library": name, "stderr": error})
    return records


@requires(docker.DOCKER, PYTHON_ONLY_TARGETS)
def test_prepared_no_r_docker_image(binary: Path) -> list:
    return prepared_no_r_docker_image(binary, no_sandbox=True)


@requires(docker.DOCKER, PYTHON_ONLY_TARGETS)
def test_prepared_no_r_docker_image_with_sandbox(binary: Path) -> list:
    return prepared_no_r_docker_image(binary, no_sandbox=False)


def prepared_no_r_docker_image(binary: Path, *, no_sandbox: bool) -> list:
    with docker.workspace() as root:
        reference = docker.image()
        inspection = docker.docker("image", "inspect", reference)
        assert inspection.returncode == 0, inspection.stderr
        digests = json.loads(inspection.stdout)[0]["RepoDigests"]
        docker.configure(root, reference)
        environment = {
            **os.environ,
            "R_HOME": "/controller-r-must-not-be-used",
            "RETICULATE_PYTHON": "/controller-python-must-not-be-used",
        }
        arguments = ("serve", "--no-sandbox") if no_sandbox else ("serve",)
        with McpClient(binary, arguments, environment, root) as client:
            exercise_no_r_catalog(client, managed=False)
            client.send(python="print(Path('/etc/hostname').read_text().strip())")
            first = last_result_text(client).strip()
            client.transcript[-1]["result"]["content"][0]["text"] = (
                "<first container>\n"
            )
            client.send(control="restart")
            docker.absent(first)
            client.send(
                # fmt: python
                python=code("""
                    from pathlib import Path

                    print(Path("/etc/hostname").read_text().strip())
                    """)
            )
            second = last_result_text(client).strip()
            assert second != first
            client.transcript[-1]["result"]["content"][0]["text"] = (
                "<second container>\n"
            )
            transcript = client.finish()
        docker.absent(second)
        recorded = json.dumps(transcript)
        for digest in digests:
            recorded = recorded.replace(digest, "<digest>")
        return json.loads(recorded.replace(reference, "<image>"))


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
