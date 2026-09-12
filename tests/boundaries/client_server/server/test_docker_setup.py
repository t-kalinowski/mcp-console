#!/usr/bin/env -S uv run --script
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text, wait_for_evaluation_output
from support.client import McpClient
from support.docker import (
    DOCKER,
    ROOT,
    absent,
    cli_peer,
    configure,
    docker,
    image,
    normalize_recording,
    tagged_image,
    workspace,
)
from support.records import Transcript
from support.normalization import code
from support.requirements import requires
from support.suites import run_this_suite


def calls(root: Path) -> list[list[str]]:
    return [
        json.loads(line)["args"]
        for line in (root / "peer/calls").read_text().splitlines()
    ]


@requires(DOCKER)
def test_build_and_image_defaults_captured_once(binary: Path) -> Transcript:
    reference = image()
    with workspace() as root, tagged_image(reference) as base:
        context = root / "context space"
        context.mkdir()
        (context / "included").write_text("from context")
        (context / "ignored").write_text("excluded by dockerignore")
        (context / ".dockerignore").write_text("ignored\n")
        dockerfile = root / "separate Dockerfile"
        dockerfile.write_text(f"""FROM {base}
COPY . /build-input/
ENV IMAGE_VALUE=initial
LABEL org.mcp-console.test={json.dumps(root.name)}
USER 1234:1234
ENTRYPOINT ["/bin/false"]
HEALTHCHECK CMD exit 1
STOPSIGNAL SIGKILL
""")
        environment = cli_peer(root / "peer")
        docker_config = root / "docker config"
        docker_config.mkdir()
        environment["DOCKER_CONFIG"] = str(docker_config)
        config = configure(
            root,
            reference,
            compute={
                "kind": "docker",
                "build": {
                    "context": "context space",
                    "dockerfile": "separate Dockerfile",
                },
            },
        )
        with McpClient(binary, ("serve",), environment, root) as client:
            client.initialize_and_list_tools()
            client.send(
                python=code("""
                import os
                from pathlib import Path
                assert os.getuid() == 1234
                assert os.environ["IMAGE_VALUE"] == "initial"
                assert Path("/build-input/included").read_text() == "from context"
                assert not Path("/build-input/ignored").exists()
                print(Path("/etc/hostname").read_text().strip())
            """)
            )
            first = last_result_text(client).strip()
            state = json.loads(docker("inspect", first).stdout)[0]
            assert state["Config"]["Entrypoint"] == ["mcp-console"]
            assert state["Config"]["Healthcheck"]["Test"] == ["NONE"]
            assert state["Config"]["StopSignal"] == "SIGTERM"
            captured = state["Image"]
            dockerfile.write_text("FROM must-not-build-again\n")
            (context / "included").unlink()
            config.write_text("invalid: [")
            (docker_config / "config.json").write_text(
                json.dumps({"currentContext": "must-not-select-again"})
            )
            client.send(control="restart")
            absent(first)
            client.send(
                python=code("""
                import os
                from pathlib import Path
                print(os.environ["IMAGE_VALUE"])
                print(Path("/build-input/included").read_text())
                print(Path("/etc/hostname").read_text().strip())
            """)
            )
            text = last_result_text(client)
            assert text.startswith("initial\nfrom context\n"), text
            second = text.splitlines()[-1]
            assert json.loads(docker("inspect", second).stdout)[0]["Image"] == captured
            transcript, diagnostics = client.finish_with_standard_error()
            assert diagnostics, (
                "build diagnostics must be delivered separately from MCP stdout"
            )
        absent(second)
        assert sum("build" in args for args in calls(root)) == 1
        assert all(captured in args for args in calls(root) if "create" in args)
        result = docker("image", "rm", captured)
        assert result.returncode == 0, result.stderr
        return normalize_recording(transcript[3:], root) + [
            {
                "builds": 1,
                "separate_dockerfile": True,
                "dockerignore": True,
                "image_user": 1234,
                "entrypoint_healthcheck_and_stop_signal_overridden": True,
                "replacement_uses_captured_image": True,
            }
        ]


@requires(DOCKER)
def test_pull_policies_and_tag_capture(binary: Path) -> Transcript:
    reference = image()
    records = []
    for policy, present, pulls in (
        ("never", False, 0),
        ("never", True, 0),
        ("if_missing", False, 1),
        ("if_missing", True, 0),
        ("always", True, 1),
    ):
        with workspace() as root:
            tag = "mcp-console-pull-test:" + root.name.replace(" ", "-")
            environment = cli_peer(root / "peer")
            (root / "peer/mode").write_text("registry-peer")
            (root / "peer/source-image").write_text(reference)
            if present:
                assert docker("image", "tag", reference, tag).returncode == 0
            configure(
                root, tag, compute={"kind": "docker", "image": tag, "pull": policy}
            )
            try:
                with McpClient(binary, ("serve",), environment, root) as client:
                    if policy == "never" and not present:
                        assert client.stdout.read(timeout=20) == ""
                        diagnostics = client.stderr.read(timeout=20)
                        assert "No such image" in diagnostics
                        transcript = []
                        assert client.process.wait(timeout=5) != 0
                    else:
                        client.initialize_and_list_tools()
                        client.send(r="42")
                        assert last_result_text(client) == "[1] 42\n"
                        # Removing the mutable name cannot invalidate captured generations.
                        assert docker("image", "rm", tag).returncode == 0
                        client.send(control="restart")
                        client.send(r="42")
                        assert last_result_text(client) == "[1] 42\n"
                        transcript, diagnostics = client.finish_with_standard_error()
                        transcript = transcript[3:]
                        assert diagnostics == "registry peer: pulled image\n" * pulls, (
                            diagnostics
                        )
                operations = calls(root)
                assert sum("pull" in args for args in operations) == pulls, operations
                assert all(reference in args for args in operations if "create" in args)
                records.append(
                    {
                        "pull": policy,
                        "initially_present": present,
                        "pull_count": pulls,
                        "stderr": diagnostics.replace(tag, "<requested image>"),
                    }
                )
                records.extend(normalize_recording(transcript, root))
            finally:
                # Only this test's unique tag; never remove the fixture image.
                if docker("image", "inspect", tag).returncode == 0:
                    assert docker("image", "rm", tag).returncode == 0
    return records


@requires(DOCKER)
def test_setup_failures_retire_containers(binary: Path) -> Transcript:
    reference = image()
    records = []
    for case, expected in (
        ("workspace", "workspace"),
        ("mount", "bind source path does not exist"),
        ("runtime", "RETICULATE_PYTHON"),
        ("python_executable", "container Python probe failed"),
        ("compatibility", "incompatible Docker bootstrap"),
        ("native_environment", "mcp-console-sandbox: invalid configuration JSON"),
        ("native_policy", "mcp-console-sandbox: invalid configuration JSON"),
    ):
        with workspace() as root:
            environment = cli_peer(root / "peer")
            config = configure(root, reference)
            value = json.loads(config.read_text())
            if case == "workspace":
                value["target"]["workspace"] = "/missing-workspace-must-not-be-created"
            elif case == "mount":
                value["target"]["compute"]["mounts"] = [
                    {"source": "missing-bind", "target": "/workspace"}
                ]
            elif case == "runtime":
                value["sandbox"]["environment"] = {
                    "RETICULATE_PYTHON": "/missing-python"
                }
            elif case == "python_executable":
                value["sandbox"]["environment"] = {"RETICULATE_PYTHON": "/etc/hostname"}
            elif case == "compatibility":
                program = root / "incompatible.py"
                program.write_text("""import json, struct, sys
payload = json.dumps({"version": 999, "build": "incompatible"}).encode()
sys.stdout.buffer.write(bytes([1]) + struct.pack(">I", len(payload)) + payload)
sys.stdout.buffer.flush()
""")
                value["target"]["command"] = ["/opt/analysis/bin/python", "/peer.py"]
                value["target"]["compute"]["mounts"] = [
                    {"source": str(program), "target": "/peer.py"}
                ]
            elif case == "native_environment":
                value["sandbox"]["environment"] = {"R_HOME": 123}
            elif case == "native_policy":
                value["sandbox"]["filesystem"] = {"kind": "native-runner-must-validate"}
            config.write_text(json.dumps(value))
            with McpClient(binary, ("serve",), environment, root) as client:
                assert client.stdout.read(timeout=30) == ""
                error = client.stderr.read(timeout=30)
                assert expected in error, error
                assert client.process.wait(timeout=5) != 0
            assert not any(
                line.startswith("Docker command failed") and line.endswith(": ")
                for line in error.splitlines()
            ), error
            for args in calls(root):
                if "create" in args:
                    name = args[args.index("--name") + 1]
                    absent(name)
                    error = error.replace(name, "<owned container>")
            assert not (root / "missing-bind").exists()
            records.append(
                {
                    "rejected_before_readiness": case,
                    "container_absent": True,
                    "stderr": error.replace(str(root), "<controller>"),
                }
            )
    return records


@requires(DOCKER)
def test_callbacks_cannot_prepare_controller_packages(binary: Path) -> Transcript:
    reference = image()
    with workspace() as root:
        environment = cli_peer(root / "peer")
        for command in ("R", "Rscript", "uv", "uvx", "ir", "python", "python3"):
            trap = root / "peer" / command
            trap.write_text(
                code("""
                #!/bin/sh
                printf called >> "$CONSOLE_DOCKER_PEER/resolver-called"
                exit 93
            """)
            )
            trap.chmod(0o755)
        configure(
            root,
            reference,
            command=["/opt/analysis/bin/python", "/target.py", "callbacks"],
            mounts=[
                {
                    "source": str(ROOT / "tests/fixtures/docker_target.py"),
                    "target": "/target.py",
                }
            ],
        )
        with McpClient(binary, ("serve",), environment, root) as client:
            client.initialize_and_list_tools()
            wait_for_evaluation_output(
                client,
                "dynamic environment resolution is unavailable for Docker targets; install packages in the image and start a new server session\ndynamic environment resolution is unavailable\ndynamic environment resolution is unavailable\n",
                "disabled container callbacks",
                r="42",
            )
            result = client.finish()[3:]
        assert not (root / "peer/resolver-called").exists()
        return result


if __name__ == "__main__":
    run_this_suite(__file__)
