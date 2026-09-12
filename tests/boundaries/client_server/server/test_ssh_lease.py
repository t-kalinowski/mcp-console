#!/usr/bin/env -S uv run --script

import json
import os
from pathlib import Path
import socket
import signal
import shlex
import sys
from tempfile import TemporaryDirectory
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.client import McpClient
from support.checkpoints import FifoCheckpoint
from support.events import Events
from support.processes import (
    capture_process_identity,
    child_process_identities,
    host_process_id,
    kill_processes,
)
from support.r import r_test_environment
from support.requirements import PROCESS_EVENTS, SANDBOX, WORKER, requires
from support.ssh import SSH, configure, localhost, remote_command
from support.suites import run_this_suite


def fault(root: Path, role: str, action: str):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
        control.settimeout(5)
        control.connect(str(root / (role + ".sock")))
        control.sendall(action.encode())
        assert control.recv(100) == b"ok"


def stalled_worker(binary, direction, workload=None):
    with TemporaryDirectory() as temporary, Events() as exits:
        root = Path(temporary).resolve()
        remote = root / "remote"
        remote.mkdir()
        checkpoint = FifoCheckpoint.create(remote / "active")
        environment, _ = r_test_environment()
        prefix = remote_command(
            remote,
            binary,
            {
                "PATH": "/usr/bin:/bin",
                "R_HOME": environment["R_HOME"],
                "R_LIBS_USER": "/unavailable",
                "R_LIBS_SITE": "/unavailable",
            },
        )
        launcher = Path(prefix[0])
        launcher.write_text(
            launcher.read_text().replace(
                "#!/bin/sh\n",
                '#!/bin/sh\nif [ "$2" = ssh-launch ]; then printf "%s\\n" "$$" > '
                + shlex.quote(str(remote / "launch-owner"))
                + "; fi\n",
            )
        )
        config = configure(root, remote, prefix, extends=":workspace")
        value = json.loads(config.read_text())
        value["target"]["lease_ms"] = 1500
        config.write_text(json.dumps(value))
        with localhost(root / "sshd", faults=True) as controller:
            with McpClient(binary, ("serve",), controller, root) as client:
                client.initialize_and_list_tools()
                server = capture_process_identity(client.process.pid)
                before = child_process_identities(server)
                client.send(r="cat(Sys.getpid(), Sys.getenv('TMPDIR'), sep='\\n')")
                assert len(last_result_text(client).splitlines()) == 2, (
                    last_result_text(client),
                    client._diagnostics(),
                )
                pid, private = last_result_text(client).splitlines()
                identity = capture_process_identity(
                    host_process_id(int(pid), os.getpid())
                )
                owner = capture_process_identity(
                    int((remote / "launch-owner").read_text())
                )
                controller_pid = [
                    child[0]
                    for child in child_process_identities(server)
                    if child not in before
                ][0]
                exits.watch_process(identity[0])
                exits.watch_process(owner[0])
                try:
                    if workload is not None:
                        client.send(
                            r=(
                                "con <- fifo("
                                + json.dumps(str(checkpoint.path))
                                + ", open='wb', blocking=TRUE); "
                                "writeBin(as.raw(49), con); close(con); " + workload
                            ),
                            timeout_ms=0,
                        )
                        checkpoint.wait("remote evaluation entered its workload")
                    # Freeze only the local lease adapter: it cannot turn its
                    # own watchdog expiry into SSH EOF and mask remote expiry.
                    os.kill(controller_pid, signal.SIGSTOP)
                    fault(root / "sshd", "ssh-launch", direction)
                    deadline = time.monotonic() + 9
                    pending = {identity[0], owner[0]}
                    while pending:
                        remaining = deadline - time.monotonic()
                        assert remaining > 0, "remote lease did not retire worker"
                        observed = exits.wait(remaining)
                        assert observed, "remote lease did not retire worker"
                        pending.difference_update(observed)
                    os.kill(controller_pid, signal.SIGCONT)
                    client.send()
                    client.send(control="restart")
                    assert "unconfirmed" in last_result_text(client), last_result_text(
                        client
                    )
                    assert not Path(private).exists()
                    return [
                        {
                            "stalled_direction": direction,
                            "remote_worker_retired": True,
                            "private_storage_removed": True,
                            "replacement_blocked": True,
                        }
                    ]
                finally:
                    try:
                        os.kill(controller_pid, signal.SIGCONT)
                    except ProcessLookupError:
                        pass
                    kill_processes([identity])
                    checkpoint.close()


@requires(SSH, WORKER, SANDBOX, PROCESS_EVENTS)
def test_stalled_controller_to_worker_expires(binary):
    return stalled_worker(binary, "up")


@requires(SSH, WORKER, SANDBOX, PROCESS_EVENTS)
def test_stalled_worker_to_controller_expires(binary):
    return stalled_worker(binary, "down")


@requires(SSH, WORKER, SANDBOX, PROCESS_EVENTS)
def test_stalled_worker_connection_expires(binary):
    return stalled_worker(binary, "up down")


@requires(SSH, WORKER, SANDBOX, PROCESS_EVENTS)
def test_busy_worker_expires(binary):
    return stalled_worker(binary, "up down", "Sys.sleep(60)")


@requires(SSH, WORKER, SANDBOX, PROCESS_EVENTS)
def test_input_prompt_expires(binary):
    return stalled_worker(binary, "up down", "readline('lease prompt: ')")


@requires(SSH, WORKER, SANDBOX, PROCESS_EVENTS)
def test_output_producer_expires(binary):
    return stalled_worker(binary, "up down", "repeat cat(strrep('x', 65536))")


if __name__ == "__main__":
    run_this_suite(__file__)
