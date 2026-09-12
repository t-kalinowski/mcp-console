#!/usr/bin/env -S uv run --script

import sys
import os
import signal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.requirements.test_ssh_control import (
    gated_session,
    observe,
    retired,
)
from boundaries.client_server.server.test_ssh_lease import fault
from support.assertions import last_result_text
from support.processes import capture_process_identity, child_process_identities
from support.requirements import WORKER, PROCESS_EVENTS, command, requires
from support.ssh import SSH
from support.suites import run_this_suite


def stalled_preparation(binary, direction, probe=False):
    with gated_session(binary, probe=probe, lease_ms=1500, faults=True) as (
        client,
        remote,
        started,
        release,
        exits,
        identities,
    ):
        if not probe:
            client.send(r="must_not_execute <- TRUE", timeout_ms=0)
        owner = observe(remote, started, exits, identities)
        exits.watch_process(owner[0])
        proxy = child_process_identities(capture_process_identity(client.process.pid))[
            0
        ]
        os.kill(proxy[0], signal.SIGSTOP)
        try:
            fault(remote.parent / "sshd", "ssh-prepare", direction)
            retired(exits, [*identities, owner])
        finally:
            os.kill(proxy[0], signal.SIGCONT)
        if not probe:
            client.send()
            assert "unconfirmed" in last_result_text(client), last_result_text(client)
            client.send(control="restart", r="must_not_replace <- TRUE")
            assert "unconfirmed" in last_result_text(client), last_result_text(client)
        else:
            assert client.process.wait(timeout=10) != 0
            assert not client.stdout.read()
            assert "unconfirmed" in client.stderr.read()
        return [
            {
                "stalled_direction": direction,
                "during_discovery": probe,
                "resolver_and_descendant_retired": True,
                "completion_unconfirmed": True,
            }
        ]


@requires(SSH, WORKER, PROCESS_EVENTS, command("ir"), command("uv"))
def test_stalled_preparation_input_expires(binary):
    return stalled_preparation(binary, "up")


@requires(SSH, WORKER, PROCESS_EVENTS, command("ir"), command("uv"))
def test_stalled_preparation_output_expires(binary):
    return stalled_preparation(binary, "down")


@requires(SSH, WORKER, PROCESS_EVENTS, command("ir"), command("uv"))
def test_stalled_discovery_expires(binary):
    return stalled_preparation(binary, "up down", probe=True)


if __name__ == "__main__":
    run_this_suite(__file__)
