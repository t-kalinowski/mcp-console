#!/usr/bin/env -S uv run --script
import json
import os
import signal
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.checkpoints import FifoCheckpoint
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
    removal_event,
    workspace,
)
from support.normalization import code
from support.records import Transcript
from support.requirements import POSIX, requires
from support.suites import run_this_suite


@requires(POSIX)
def test_cancelled_image_setup_before_readiness(binary: Path) -> Transcript:
    with workspace() as root:
        environment = cli_peer(root / "peer")
        (root / "peer/mode").write_text("setup-gate")
        configure(
            root,
            "fixture:image",
            compute={"kind": "docker", "image": "fixture:image", "pull": "always"},
        )
        with closing(FifoCheckpoint.create(root / "peer/reached")) as reached:
            with McpClient(binary, ("serve",), environment, root) as client:
                reached.wait("image pull admitted before MCP readiness")
                client.stdin.close()
                assert client.stdout.read(timeout=15) == ""
                error = client.stderr.read(timeout=15)
                assert "cancel" in error, error
                assert client.process.wait(timeout=5) != 0
        calls = [
            json.loads(line)["args"]
            for line in (root / "peer/calls").read_text().splitlines()
        ]
        assert not any("create" in args for args in calls), calls
        return [
            {"cancelled_before_readiness": True, "container_creation_started": False}
        ]


@requires(DOCKER)
def test_cancelled_creation_uses_ownership_token(binary: Path) -> Transcript:
    reference = image()
    with workspace() as root:
        environment = cli_peer(root / "peer")
        (root / "peer/mode").write_text("create-gate")
        configure(root, reference)
        with closing(FifoCheckpoint.create(root / "peer/reached")) as reached:
            with McpClient(binary, ("serve",), environment, root) as client:
                reached.wait("container created before ID delivery", timeout=30)
                identity = (root / "peer/created").read_text().strip()
                client.stdin.close()
                assert client.stdout.read(timeout=15) == ""
                error = client.stderr.read(timeout=15)
                assert "cancel" in error, error
                assert client.process.wait(timeout=5) != 0
        absent(identity)
        return [
            {
                "cancelled_after_create_before_id_delivery": True,
                "container_absent": True,
            }
        ]


@requires(DOCKER)
def test_cancelled_real_build_stops_setup_container(binary: Path) -> Transcript:
    reference = image()
    with workspace() as root:
        marker = "build-cancel-" + root.name.replace(" ", "-")
        dockerfile = root / "Dockerfile"
        dockerfile.write_text(f"""FROM {reference}
LABEL org.mcp-console.build-cancel={marker}
RUN echo {marker} && exec sleep 3600
""")
        configure(
            root,
            reference,
            compute={
                "kind": "docker",
                "build": {"context": ".", "dockerfile": "Dockerfile"},
            },
        )
        identity = None
        try:
            with McpClient(
                binary, ("serve",), {**os.environ, "DOCKER_BUILDKIT": "0"}, root
            ) as client:
                while True:
                    line = client.stderr.readline(timeout=30)
                    assert line, "build stopped before its execution checkpoint"
                    if line.strip() == marker:
                        break
                result = docker(
                    "container",
                    "ls",
                    "--quiet",
                    "--filter",
                    f"label=org.mcp-console.build-cancel={marker}",
                )
                assert result.returncode == 0 and result.stdout.strip(), result
                identity = result.stdout.strip()
                with removal_event(identity) as removed:
                    client.stdin.close()
                    assert client.stdout.read(timeout=15) == ""
                    error = client.stderr.read(timeout=15)
                    assert "cancel" in error, error
                    assert client.process.wait(timeout=5) != 0
                    removed()
                absent(identity)
        finally:
            if identity is not None and docker("inspect", identity).returncode == 0:
                assert docker("rm", "--force", identity).returncode == 0
        return [
            {
                "build_started": True,
                "startup_cancelled": True,
                "build_container_absent": True,
                "analysis_worker_started": False,
            }
        ]


@requires(DOCKER)
def test_cancelled_probe_and_pre_ready_launch(binary: Path) -> Transcript:
    reference = image()
    records = []
    for mode in ("probe-gate", "launch-gate"):
        with workspace() as root:
            environment = cli_peer(root / "peer")
            configure(
                root,
                reference,
                command=["/opt/analysis/bin/python", "/target.py", mode],
                mounts=[
                    {
                        "source": str(ROOT / "tests/fixtures/docker_target.py"),
                        "target": "/target.py",
                    }
                ],
            )
            with McpClient(binary, ("serve",), environment, root) as client:
                if mode == "launch-gate":
                    client.initialize_and_list_tools()
                    client.start_request(
                        "tools/call",
                        name="send",
                        arguments={"r": "must_not_run <- TRUE"},
                    )
                assert client.stderr.readline(timeout=30) == "target launch gate\n"
                client.stdin.close()
                client.stdout.read(timeout=15)
                client.stderr.read(timeout=15)
                client.process.wait(timeout=5)
            for line in (root / "peer/calls").read_text().splitlines():
                args = json.loads(line)["args"]
                if "create" in args:
                    absent(args[args.index("--name") + 1])
            records.append({"cancelled": mode, "owned_containers_absent": True})
    return records


@requires(DOCKER)
def test_unacknowledged_creation_is_not_an_absence_receipt(binary: Path) -> Transcript:
    reference = image()
    with workspace() as root:
        environment = cli_peer(root / "peer")
        (root / "peer/mode").write_text("create-unacknowledged")
        configure(root, reference)
        with closing(FifoCheckpoint.create(root / "peer/reached")) as reached:
            with McpClient(binary, ("serve",), environment, root) as client:
                reached.wait("creation request without a daemon result", timeout=30)
                client.stdin.close()
                assert client.stdout.read(timeout=15) == ""
                error = client.stderr.read(timeout=15)
                assert "retirement is unconfirmed" in error, error
                assert "creation ID unavailable" in error, error
                assert client.process.wait(timeout=5) != 0
        return [
            {
                "unacknowledged_creation": True,
                "empty_listing_is_not_a_cleanup_receipt": True,
            }
        ]


def _loss(binary: Path, victim: str) -> Transcript:
    reference = image()
    with workspace() as root:
        environment = cli_peer(root / "peer")
        configure(root, reference)
        if victim == "process_group_interrupt":
            launcher = root / "server"
            launcher.write_text(
                code(f"""
                #!{sys.executable}
                import os, sys
                os.setsid()
                os.execv({str(binary)!r}, [{str(binary)!r}, *sys.argv[1:]])
            """)
            )
            launcher.chmod(0o755)
            binary = launcher
        with McpClient(binary, ("serve", "--no-sandbox"), environment, root) as client:
            client.initialize_and_list_tools()
            client.send(
                python=code("""
                import os, sys, subprocess
                from pathlib import Path
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], start_new_session=True)
                print(Path("/etc/hostname").read_text().strip())
                """)
            )
            identity = last_result_text(client).strip()
            assert docker("inspect", identity).returncode == 0, identity
            calls = [
                json.loads(line)
                for line in (root / "peer/calls").read_text().splitlines()
            ]
            attachments = [call["pid"] for call in calls if "start" in call["args"]]
            assert len(attachments) == 2, calls
            with removal_event(identity) as removed:
                if victim == "process_group_interrupt":
                    os.killpg(client.process.pid, signal.SIGINT)
                elif victim == "server":
                    client.process.kill()
                else:
                    os.kill(attachments[-1], signal.SIGKILL)
                removed()
            absent(identity)
            if victim in ("server", "process_group_interrupt"):
                client.process.wait(timeout=10)
            else:
                client.send(control="restart")
                client.send(r="42")
                assert last_result_text(client) == "[1] 42\n", last_result_text(client)
                client.finish()
        return normalize_recording(client.transcript[3:], root) + [
            {"lost": victim, "escaped_output_writer": True, "container_absent": True}
        ]


@requires(DOCKER)
def test_server_loss_retires_container_and_inherited_writers(
    binary: Path,
) -> Transcript:
    return _loss(binary, "server")


@requires(DOCKER)
def test_attachment_loss_retires_container_before_replacement(
    binary: Path,
) -> Transcript:
    return _loss(binary, "attachment")


@requires(DOCKER)
def test_process_group_interrupt_retires_container(binary: Path) -> Transcript:
    return _loss(binary, "process_group_interrupt")


@requires(DOCKER)
def test_daemon_failure_blocks_replacement(binary: Path) -> Transcript:
    return _daemon_failure(binary, "cleanup-loss")


@requires(DOCKER)
def test_unresponsive_daemon_has_bounded_retirement(binary: Path) -> Transcript:
    return _daemon_failure(binary, "cleanup-hang")


def _daemon_failure(binary: Path, mode: str) -> Transcript:
    reference = image()
    with workspace() as root:
        environment = cli_peer(root / "peer")
        configure(root, reference)
        identity = None
        try:
            with McpClient(binary, ("serve",), environment, root) as client:
                client.initialize_and_list_tools()
                client.send(r='cat(readLines("/etc/hostname"))')
                identity = last_result_text(client).strip()
                assert docker("inspect", identity).returncode == 0, identity
                (root / "peer/mode").write_text(mode)
                client.response_timeout = 15
                client.send(control="restart", r="must_not_run <- TRUE")
                assert "unconfirmed" in last_result_text(client), last_result_text(
                    client
                )
                client.send(control="restart", r="must_not_run <- TRUE")
                calls = [
                    json.loads(line)
                    for line in (root / "peer/calls").read_text().splitlines()
                ]
                assert sum("create" in call["args"] for call in calls) == 2, calls
                client.stdin.close()
                client.stdout.read(timeout=15)
                client.stderr.read(timeout=15)
                client.process.wait(timeout=5)
        finally:
            if identity is not None:
                result = docker("rm", "--force", identity)
                assert result.returncode == 0, result.stderr
        return normalize_recording(client.transcript[3:], root) + [
            {"daemon_cleanup_receipt_lost": True, "replacement_blocked": True}
        ]


@requires(DOCKER)
def test_worker_failure_retires_container_without_replaying_cell(
    binary: Path,
) -> Transcript:
    reference = image()
    with workspace() as root:
        configure(
            root,
            reference,
            mounts=[{"source": ".", "target": "/workspace", "access": "read_write"}],
        )
        with McpClient(binary, ("serve",), current_directory=root) as client:
            client.initialize_and_list_tools()
            client.send(
                python='from pathlib import Path; print(Path("/etc/hostname").read_text().strip())'
            )
            first = last_result_text(client).strip()
            with removal_event(first) as removed:
                client.send(
                    python=code("""
                    import os
                    with open("submissions", "a") as stream:
                        stream.write("once\\n")
                    os._exit(23)
                """)
                )
                removed()
            absent(first)
            client.send(r="42")
            assert last_result_text(client) == "[1] 42\n", last_result_text(client)
            transcript = client.finish()[3:]
        assert (root / "submissions").read_text() == "once\n"
        return json.loads(json.dumps(transcript).replace(first, "<container>")) + [
            {
                "worker_failed": True,
                "old_container_absent": True,
                "submitted_cell_executions": 1,
                "replacement_ready": True,
            }
        ]


if __name__ == "__main__":
    run_this_suite(__file__)
