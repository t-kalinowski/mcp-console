#!/usr/bin/env -S uv run --script

import json
import os
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text, last_tool_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.events import Events
from support.execution import DIRECT
from support.normalization import code
from support.processes import (
    capture_process_identity,
    child_process_identities,
    kill_processes,
    live_processes,
)
from support.r import r_test_environment
from support.resolvers import send_and_collect_runtime_python_resolution
from support.requirements import (
    WORKER,
    PROCESS_EVENTS,
    NATIVE_FIXTURES,
    command,
    requires,
)
from support.native import LOADER_VARIABLE, build_interposer
from support.ssh import (
    SSH,
    configure,
    localhost,
    remote_command,
    poison_controller,
    read_exact,
)
from support.suites import run_this_suite


@contextmanager
def gated_session(binary: Path, *, probe=False, advance_clock=False):
    with TemporaryDirectory() as temporary, Events() as exits:
        root = Path(temporary).resolve()
        local, remote = root / "local", root / "remote"
        local.mkdir()
        remote.mkdir()
        started = FifoCheckpoint.create(remote / "started")
        release = FifoCheckpoint.create(remote / "release")
        r_environment, _ = r_test_environment()
        environment = {
            name: value
            for name, value in r_environment.items()
            if name
            in {
                "HOME",
                "PATH",
                "R_HOME",
                "R_LIBS",
                "R_LIBS_SITE",
                "R_LIBS_USER",
                "R_PROFILE_USER",
                "IR_CACHE_DIR",
                "RENV_PATHS_CACHE",
                "UV_CACHE_DIR",
            }
        }
        fake_bin = remote / "bin"
        fake_bin.mkdir()
        fixture = Path(__file__).resolve().parents[3] / "fixtures/startup_ir"
        (fake_bin / "ir").symlink_to(fixture)
        if probe:
            environment.pop("R_HOME", None)
            r = fake_bin / "R"
            r.write_text(
                "#!/bin/sh\nexec "
                + shlex.join([sys.executable, str(fixture), "--version"])
                + "\n"
            )
            r.chmod(0o755)
        environment["PATH"] = str(fake_bin) + os.pathsep + environment["PATH"]
        environment.update(
            {
                "MCP_CONSOLE_TEST_REAL_IR": shutil.which("ir"),
                "MCP_CONSOLE_TEST_STARTUP_PHASE": "all",
                "MCP_CONSOLE_TEST_STARTUP_CLAIM": str(remote / "claimed"),
                "MCP_CONSOLE_TEST_STARTUP_IDENTITY": str(remote / "identity"),
                "MCP_CONSOLE_TEST_STARTUP_STARTED": str(started.path),
                "MCP_CONSOLE_TEST_STARTUP_RELEASE": str(release.path),
            }
        )
        prefix = remote_command(remote, binary, environment)
        launcher = Path(prefix[0])
        launcher.write_text(
            launcher.read_text().replace(
                "#!/bin/sh\n",
                code(r"""
                    #!/bin/sh
                    if [ "$1" = ssh-prepare ]; then
                      printf '%s\n' "$$" > OWNER
                    fi
                    """).replace(
                    "OWNER", shlex.quote(str(remote / "preparation-owner"))
                ),
            )
        )
        configure(local, remote, prefix)
        with localhost(root / "sshd") as controller:
            poison_controller(root / "sshd", controller)
            if advance_clock:
                controller.update(
                    {
                        LOADER_VARIABLE: str(
                            build_interposer(local, "relay_completed_output")
                        ),
                        "MCP_CONSOLE_TEST_CLOCK_AFTER_FRAME": r'"text":"\n[running; poll with an empty send]"',
                        "MCP_CONSOLE_TEST_OUTPUT_COMPLETE": str(
                            remote / "clock-advanced"
                        ),
                        "MCP_CONSOLE_TEST_CLOCK_SECONDS": "60",
                    }
                )
            client = McpClient(
                binary, DIRECT.serve(), controller, local, response_timeout=15
            )
            identities = []
            try:
                if not probe:
                    client.initialize_and_list_tools()
                yield client, remote, started, release, exits, identities
            finally:
                client.close()
                kill_processes(identities)
                started.close()
                release.close()


def observe(remote, started, exits, identities):
    started.wait("remote resolver and descendant reached checkpoint")
    for pid in map(int, (remote / "identity").read_text().split()):
        identities.append(capture_process_identity(pid))
        exits.watch_process(pid)
    return capture_process_identity(int((remote / "preparation-owner").read_text()))


def exited(exits, identities):
    pending = {identity[0] for identity in identities}
    deadline = time.monotonic() + 6
    while pending:
        remaining = deadline - time.monotonic()
        assert remaining > 0, pending
        observed = exits.wait(remaining)
        assert observed, pending
        pending.difference_update(observed)


def retired(exits, identities):
    exited(exits, identities)
    # Completion receipts (or the owner's exit after transport loss) follow
    # reaping the direct resolver. Its orphaned descendant's exit event proves
    # termination without assuming when the system reaper removes its PID.
    assert not live_processes(identities[:1]), identities


@requires(SSH, WORKER, PROCESS_EVENTS, command("ir"), command("uv"))
def test_lazy_preparation_remains_pollable_and_interruptible(binary):
    with gated_session(binary) as (client, remote, started, release, exits, identities):
        client.send(r="42L", timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        observe(remote, started, exits, identities)
        client.request("ping")
        client.send(timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        client.send(control="interrupt")
        retired(exits, identities)
        assert (
            last_result_text(client)
            == "[failed to check R package resolver version with exit status: 130: ]"
        ), last_result_text(client)
        client.response_timeout = 180
        send_and_collect_runtime_python_resolution(client, r="42L")
        assert last_tool_text(client).endswith("[1] 42\n"), last_tool_text(client)
        return client.finish()[3:]


@requires(SSH, WORKER, PROCESS_EVENTS, command("ir"), command("uv"))
def test_restart_cancels_remote_preparation_before_replacement(binary):
    with gated_session(binary) as (client, remote, started, release, exits, identities):
        client.send(r="old_generation <- TRUE", timeout_ms=0)
        observe(remote, started, exits, identities)
        restart = client.start_send(control="restart")
        # Retirement must finish promptly even if the replacement still needs
        # to install its defaults. Installation has its ordinary response wait.
        exited(exits, identities)
        client.response_timeout = 180
        client.receive(restart)
        assert not live_processes(identities[:1]), identities
        client.send(r='stopifnot(!exists("old_generation")); 42L')
        assert last_tool_text(client).endswith("[1] 42\n"), last_tool_text(client)
        return client.finish()[3:]


@requires(SSH, WORKER, PROCESS_EVENTS, NATIVE_FIXTURES, command("ir"), command("uv"))
def test_preparation_outlives_the_setup_deadline(binary):
    with gated_session(binary, advance_clock=True) as (
        client,
        remote,
        started,
        release,
        exits,
        identities,
    ):
        client.send(r="42L", timeout_ms=0)
        observe(remote, started, exits, identities)
        # MCP output advances the controller's monotonic clock by 60 seconds.
        # Control then wakes the preparation owner after its old setup deadline.
        client.request("ping")
        completed = (remote / "clock-advanced").read_text()
        assert completed == "1", repr(completed)
        client.send(control="interrupt")
        retired(exits, identities)
        assert "exit status: 130" in last_result_text(client), last_result_text(client)
        return client.finish()[3:]


@requires(SSH, WORKER, PROCESS_EVENTS, command("ir"), command("uv"))
def test_explicit_preparation_waits_and_accepts_concurrent_control(binary):
    with gated_session(binary) as (client, remote, started, release, exits, identities):
        preparation = client.start_send(requirements={"r": ["praise"]}, timeout_ms=0)
        observe(remote, started, exits, identities)
        # The ping response precedes any preparation response even with timeout 0.
        client.request("ping")
        interrupt = client.start_send(control="interrupt")
        client.receive_many([preparation, interrupt])
        retired(exits, identities)
        assert "exit status: 130" in json.dumps(preparation), preparation
        return client.finish()[3:]


@requires(SSH, WORKER, PROCESS_EVENTS, command("ir"), command("uv"))
def test_input_closure_cancels_remote_preparation(binary):
    with gated_session(binary) as (client, remote, started, release, exits, identities):
        client.send(r="must_not_run <- TRUE", timeout_ms=0)
        observe(remote, started, exits, identities)
        client.stdin.close()
        assert client.process.wait(timeout=10) == 0
        retired(exits, identities)
        return [
            {
                "stdout": client.stdout.read(),
                "stderr": client.stderr.read(),
                "remote_resolver_and_descendant_retired": True,
            }
        ]


@requires(SSH, WORKER, PROCESS_EVENTS, command("ir"), command("uv"))
def test_input_closure_cancels_discovery_before_mcp_ready(binary):
    with gated_session(binary, probe=True) as (
        client,
        remote,
        started,
        release,
        exits,
        identities,
    ):
        observe(remote, started, exits, identities)
        client.stdin.close()
        assert client.process.wait(timeout=10) != 0
        retired(exits, identities)
        assert not client.stdout.read()
        errors = client.stderr.read()
        assert "closed" in errors or "cancelled" in errors, errors
        return [
            {
                "stdout": "",
                "stderr": errors,
                "remote_probe_and_descendant_retired": True,
            }
        ]


@requires(SSH, WORKER, PROCESS_EVENTS, command("ir"), command("uv"))
def test_detected_transport_loss_blocks_preparation_and_replacement(binary):
    with gated_session(binary) as (client, remote, started, release, exits, identities):
        client.send(r="must_not_run <- TRUE", timeout_ms=0)
        owner = observe(remote, started, exits, identities)
        exits.watch_process(owner[0])
        children = child_process_identities(
            capture_process_identity(client.process.pid)
        )
        assert len(children) == 1, children
        os.kill(children[0][0], signal.SIGKILL)
        client.send()
        assert "unconfirmed" in last_result_text(client), last_result_text(client)
        retired(exits, [*identities, owner])
        client.send(control="restart", r="must_not_replace <- TRUE")
        assert "unconfirmed" in last_result_text(client), last_result_text(client)
        client.stdin.close()
        client.process.wait(timeout=10)
        return client.transcript[3:] + [{"stderr": client.stderr.read()}]


@requires(SSH, WORKER, PROCESS_EVENTS, NATIVE_FIXTURES, command("ir"), command("uv"))
def test_connection_closure_reaps_preparation_with_backpressured_output(binary):
    with gated_session(binary) as (client, remote, started, release, exits, identities):
        blocked = FifoCheckpoint.create(remote / "stdout-blocked")
        interposer = build_interposer(remote, "relay_stdout_backpressure")
        prefix = remote / "remote-console"
        text = prefix.read_text()
        selected = shlex.join(
            [
                "/usr/bin/env",
                f"{LOADER_VARIABLE}={interposer}",
                f"MCP_CONSOLE_TEST_STDOUT_BLOCKED={blocked.path}",
                str(binary),
            ]
        )
        prefix.write_text(text.replace(shlex.quote(str(binary)), selected))
        ssh = remote.parent / "sshd/ssh"
        process = subprocess.Popen(
            [ssh, "-T", "-a", "console-test", shlex.join([str(prefix), "ssh-prepare"])],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdin and process.stdout and process.stderr

        def frame(message):
            body = json.dumps(message).encode()
            return struct.pack(">I", len(body)) + body

        def read_message():
            deadline = time.monotonic() + 15
            length = struct.unpack(">I", read_exact(process.stdout, 4, deadline))[0]
            return json.loads(read_exact(process.stdout, length, deadline))

        try:
            version = subprocess.check_output([binary, "--version"], text=True).split()[
                1
            ]
            process.stdin.write(
                frame(
                    {
                        "Open": {
                            "version": 2,
                            "build": version,
                            "workspace": str(remote),
                            "selections": {"r_home": None, "python": None},
                        }
                    }
                )
            )
            process.stdin.flush()
            assert "Hello" in read_message()
            assert read_message()["Completed"]["id"] == 0
            process.stdin.write(frame({"Run": {"id": 1, "operation": "Bootstrap"}}))
            process.stdin.flush()
            owner = observe(remote, started, exits, identities)
            exits.watch_process(owner[0])
            # Fill SSH's output window with explicit stale-control replies,
            # then observe a real EAGAIN at the remote helper's stdout.
            flood = frame({"Control": {"id": 999, "control": "Interrupted"}}) * 300_000
            failure = []

            def write_controls():
                try:
                    process.stdin.write(flood)
                    process.stdin.flush()
                except OSError as error:
                    failure.append(error)

            writer = threading.Thread(target=write_controls, daemon=True)
            writer.start()
            blocked.wait("remote preparation stdout is backpressured")
            writer.join(timeout=15)
            assert not writer.is_alive() and not failure, failure
            process.stdin.close()
            retired(exits, [*identities, owner])
            process.stdin = None
            _, errors = process.communicate(timeout=12)
            assert process.returncode != 0
            assert b"truncated SSH frame" in errors, errors
            client.finish()
            return [
                {
                    "remote_stdout_backpressure_observed": True,
                    "resolver_reaped_and_descendant_exited_before_output_drain": True,
                }
            ]
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=12)
            blocked.close()


if __name__ == "__main__":
    run_this_suite(__file__)
