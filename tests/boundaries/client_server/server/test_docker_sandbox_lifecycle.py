#!/usr/bin/env -S uv run --script
import json
import os
import signal
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.docker_sandbox import (
    DOCKER_SANDBOX,
    DOCKER_SANDBOX_INNER_DOCKER,
    absent,
    calls,
    cli_peer,
    configure,
    finish,
    generations,
    normalize_recording,
    sbx,
    workspace,
)
from support.events import Events
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code
from support.requirements import NATIVE_FIXTURES, PROCESS_EVENTS, requires
from support.suites import run_this_suite


def remove_remaining(identity: dict) -> None:
    result = sbx("ls", "--json")
    assert result.returncode == 0, result.stderr
    if identity in [
        {"name": vm["name"], "id": vm["id"]}
        for vm in json.loads(result.stdout)["sandboxes"]
    ]:
        result = sbx("rm", "--force", identity["name"])
        assert result.returncode == 0, result.stderr
    absent(**identity)


def loss(binary: Path, victim: str) -> list:
    with workspace(real=True) as root:
        environment = cli_peer(root / "peer", real=True)
        configure(root)
        identity = None
        try:
            with McpClient(binary, ("serve",), environment, root) as client:
                client.initialize_and_list_tools()
                client.send(
                    python=code("""
                    import subprocess, sys
                    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], start_new_session=True)
                    print("detached output writer started")
                """)
                )
                assert last_result_text(client) == "detached output writer started\n"
                identity = generations(root)[0]
                attachments = [
                    call for call in calls(root) if call["args"][0] == "exec"
                ]
                assert len(attachments) == 2
                attachment = attachments[-1]
                with Events() as events:
                    events.watch_process(attachment["ppid"])
                    if victim == "controller":
                        client.process.kill()
                    elif victim == "input-eof":
                        client.stdin.close()
                    else:
                        os.kill(attachment["pid"], signal.SIGKILL)
                    assert attachment["ppid"] in events.wait(20), (
                        "ownership helper did not finish bounded retirement"
                    )
                absent(**identity)
                if victim == "exec":
                    client.send(control="restart")
                    client.send(r="42")
                    assert last_result_text(client) == "[1] 42\n", last_result_text(
                        client
                    )
                    recording = finish(client, root)[3:]
                else:
                    client.stdout.read(timeout=10)
                    diagnostics = client.stderr.read(timeout=10)
                    client.process.wait(timeout=5)
                    recording = client.transcript[3:] + [
                        {"standard_error": diagnostics}
                    ]
            return normalize_recording(recording, root) + [
                {
                    "lost": victim,
                    "detached_writer_retired_with_microvm": True,
                    "replacement_after_exec_loss": victim == "exec",
                }
            ]
        finally:
            if identity:
                remove_remaining(identity)


@requires(DOCKER_SANDBOX, PROCESS_EVENTS)
def test_controller_loss_retires_whole_microvm(binary: Path) -> list:
    return loss(binary, "controller")


@requires(DOCKER_SANDBOX, PROCESS_EVENTS)
def test_controller_eof_retires_whole_microvm(binary: Path) -> list:
    return loss(binary, "input-eof")


@requires(DOCKER_SANDBOX, PROCESS_EVENTS)
def test_killed_exec_retires_before_replacement(binary: Path) -> list:
    return loss(binary, "exec")


@requires(DOCKER_SANDBOX, NATIVE_FIXTURES, PROCESS_EVENTS)
def test_failed_exec_retires_despite_output_backpressure(binary: Path) -> list:
    with workspace(real=True) as root, TemporaryDirectory(dir="/tmp") as native:
        environment = cli_peer(root / "peer", real=True)
        environment[LOADER_VARIABLE] = str(
            build_interposer(Path(native), "docker_owner_backpressure")
        )
        configure(root)
        identity = None
        stopped = False
        with closing(FifoCheckpoint.create(root / "blocked")) as blocked:
            environment["MCP_CONSOLE_TEST_STDOUT_BLOCKED"] = str(blocked.path)
            try:
                with McpClient(binary, ("serve",), environment, root) as client:
                    client.initialize_and_list_tools()
                    client.send(
                        python=code('''
                        import os, sys, subprocess
                        os.mkfifo("/tmp/output-gate")
                        producer = """
                        import os, time
                        with open("/tmp/output-gate") as gate:
                            gate.read(1)
                        os.write(1, b"x" * (64 * 1024 * 1024))
                        time.sleep(600)
                        """
                        child = subprocess.Popen([sys.executable, "-c", producer], start_new_session=True)
                        print("producer waiting")
                    ''')
                    )
                    assert last_result_text(client) == "producer waiting\n"
                    identity = generations(root)[0]
                    attachment = [
                        call for call in calls(root) if call["args"][0] == "exec"
                    ][-1]
                    os.kill(client.process.pid, signal.SIGSTOP)
                    stopped = True
                    released = sbx(
                        "exec",
                        identity["name"],
                        "/opt/analysis/bin/python",
                        "-c",
                        'with open("/tmp/output-gate", "w") as gate: gate.write("x")',
                    )
                    assert released.returncode == 0, released.stderr
                    blocked.wait("microVM owner stdout reached EAGAIN", timeout=15)
                    with Events() as events:
                        events.watch_process(attachment["ppid"])
                        os.kill(attachment["pid"], signal.SIGKILL)
                        assert attachment["ppid"] in events.wait(20), (
                            "owner blocked on output"
                        )
                    absent(**identity)
                    os.kill(client.process.pid, signal.SIGCONT)
                    stopped = False
                    client.stdin.close()
                    client.stdout.read(timeout=20)
                    errors = client.stderr.read(timeout=20)
                    assert client.process.wait(timeout=5) != 0
                    assert (
                        "retirement is unconfirmed" in errors
                        and "cannot start a replacement" in errors
                    ), errors
                return normalize_recording(
                    [
                        {
                            "owner_stdout_backpressure_observed": True,
                            "microvm_absent_before_controller_resumes": True,
                            "stderr": errors,
                        }
                    ],
                    root,
                )
            finally:
                if stopped and client.process.poll() is None:
                    os.kill(client.process.pid, signal.SIGCONT)
                if identity:
                    remove_remaining(identity)


@requires(DOCKER_SANDBOX)
def test_cancelled_creation_and_readiness_retire_real_microvms(binary: Path) -> list:
    records = []
    for mode in ("create-gate", "probe-gate", "launch-gate"):
        with workspace(real=True) as root:
            environment = cli_peer(root / "peer", real=True)
            configure(root)
            (root / "peer/mode").write_text(mode)
            with closing(FifoCheckpoint.create(root / "peer/reached")) as reached:
                with McpClient(binary, ("serve",), environment, root) as client:
                    if mode == "launch-gate":
                        client.initialize_and_list_tools()
                        client.start_send(r="must_not_run <- TRUE")
                    reached.wait(
                        "actual provider create completed or exec admitted", timeout=30
                    )
                    client.stdin.close()
                    client.stdout.read(timeout=30)
                    errors = client.stderr.read(timeout=30)
                    client.process.wait(timeout=5)
            result = sbx("ls", "--json")
            assert result.returncode == 0, result.stderr
            names = {
                call["args"][call["args"].index("--name") + 1]
                for call in calls(root)
                if call["args"][0] == "create"
            }
            assert not any(
                vm["name"] in names for vm in json.loads(result.stdout)["sandboxes"]
            ), result.stdout
            if mode == "create-gate":
                assert "unconfirmed" in errors, errors
            records.append(
                {
                    "cancelled": mode,
                    "owned_microvms_absent": True,
                    "missing_create_acknowledgement_remains_unconfirmed": mode
                    == "create-gate",
                }
            )
    return records


@requires(DOCKER_SANDBOX)
def test_unavailable_daemon_blocks_replacement_without_unbounded_wait(
    binary: Path,
) -> list:
    records = []
    for mode in ("cleanup-loss", "cleanup-hang"):
        with workspace(real=True) as root:
            environment = cli_peer(root / "peer", real=True)
            configure(root)
            identity = None
            try:
                with McpClient(
                    binary, ("serve",), environment, root, response_timeout=25
                ) as client:
                    client.initialize_and_list_tools()
                    client.send(r="42")
                    identity = generations(root)[0]
                    (root / "peer/mode").write_text(mode)
                    client.send(control="restart", r="must_not_run <- TRUE")
                    assert "unconfirmed" in last_result_text(client), last_result_text(
                        client
                    )
                    client.send(control="restart", r="must_not_run <- TRUE")
                    assert sum(call["args"][0] == "create" for call in calls(root)) == 2
                    client.stdin.close()
                    client.stdout.read(timeout=25)
                    client.stderr.read(timeout=25)
                    client.process.wait(timeout=5)
                records += normalize_recording(client.transcript[3:], root)
            finally:
                if identity:
                    remove_remaining(identity)
    return records


@requires(DOCKER_SANDBOX)
def test_worker_exit_status_is_preserved_without_replaying_cell(binary: Path) -> list:
    with workspace(real=True) as root:
        configure(
            root,
            workspace=str(root),
            mounts=[{"source": str(root), "target": str(root), "access": "read_write"}],
        )
        with McpClient(binary, ("serve",), current_directory=root) as client:
            client.initialize_and_list_tools()
            client.send(
                python=code("""
                import os
                with open("attempt", "a") as attempts:
                    attempts.write("once\\n")
                    attempts.flush()
                os._exit(23)
            """)
            )
            assert "23" in last_result_text(client), last_result_text(client)
            first = generations(root)[0]
            client.send(control="restart")
            client.send(
                python='from pathlib import Path; print(Path("attempt").read_text())'
            )
            assert last_result_text(client) == "once\n\n", last_result_text(client)
            absent(**first)
            transcript = finish(client, root)[3:]
        for identity in generations(root):
            absent(**identity)
        return transcript


@requires(DOCKER_SANDBOX, DOCKER_SANDBOX_INNER_DOCKER)
def test_restart_retires_independently_started_process_and_container(
    binary: Path,
) -> list:
    with workspace(real=True) as root:
        configure(root)
        with McpClient(binary, ("serve",), current_directory=root) as client:
            client.initialize_and_list_tools()
            client.send(r="42")
            identity = generations(root)[0]
            independent = sbx(
                "exec",
                identity["name"],
                "/opt/analysis/bin/python",
                "-c",
                code("""
                import subprocess, sys
                from pathlib import Path
                child = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(600)"],
                    start_new_session=True, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                Path("/tmp/independent-process").write_text(str(child.pid))
            """),
            )
            assert independent.returncode == 0, independent.stderr
            container = sbx(
                "exec",
                identity["name"],
                "docker",
                "run",
                "--detach",
                "--name",
                "outside-relay",
                "busybox:1.37.0",
                "sleep",
                "600",
            )
            assert container.returncode == 0, container.stderr
            running = sbx(
                "exec",
                identity["name"],
                "docker",
                "inspect",
                "--format",
                "{{.State.Running}}",
                "outside-relay",
            )
            assert running.returncode == 0 and running.stdout.strip() == "true", running
            client.send(
                python='from pathlib import Path; pid = Path("/tmp/independent-process").read_text(); print(Path("/proc", pid).exists())'
            )
            assert last_result_text(client) == "True\n", last_result_text(client)
            client.send(control="restart")
            absent(**identity)
            client.send(
                python='from pathlib import Path; print(Path("/tmp/independent-process").exists())'
            )
            assert last_result_text(client) == "False\n", last_result_text(client)
            replacement = generations(root)[-1]
            containers = sbx(
                "exec",
                replacement["name"],
                "docker",
                "ps",
                "--all",
                "--format",
                "{{.Names}}",
            )
            assert (
                containers.returncode == 0 and "outside-relay" not in containers.stdout
            ), containers
            transcript = finish(client, root)[3:]
        for identity in generations(root):
            absent(**identity)
        return transcript + [{"independent_vm_process_and_container_retired": True}]


if __name__ == "__main__":
    run_this_suite(__file__)
