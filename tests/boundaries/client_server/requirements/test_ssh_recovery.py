#!/usr/bin/env -S uv run --script

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.requirements.test_ssh_control import (
    gated_session,
    observe,
)
from boundaries.client_server.server.test_ssh_lease import fault
from boundaries.client_server.server.test_ssh_recovery import gate
from support.assertions import last_result_text
from support.processes import live_processes, capture_process_identity
from support.resolvers import send_and_collect_runtime_python_resolution
from support.requirements import WORKER, PROCESS_EVENTS, command, requires
from support.ssh import SSH
from support.suites import run_this_suite


def preparation_recovery(binary, probe=False, direction="close"):
    with gated_session(binary, probe=probe, lease_ms=6000, faults=True) as (
        client,
        remote,
        started,
        release,
        exits,
        identities,
    ):
        if not probe:
            client.send(r="value <- 42L; value", timeout_ms=0)
        owner = observe(remote, started, exits, identities)
        before = (remote / "identity").read_text()
        fault(remote.parent / "sshd", "ssh-prepare", direction)
        release.release()
        if probe:
            client.initialize_and_list_tools()
        else:
            client.response_timeout = 180
            send_and_collect_runtime_python_resolution(client)
            assert "[1] 42\n" in last_result_text(client), (
                last_result_text(client),
                client._diagnostics(),
            )
        assert live_processes([owner]) == [owner[0]], owner
        assert (remote / "identity").read_text() == before
        return [
            {
                "during_discovery": probe,
                "fault": direction,
                "same_preparation_owner": True,
                "resolver_repeated": False,
            }
        ]


@requires(SSH, WORKER, PROCESS_EVENTS, command("ir"), command("uv"))
def test_lazy_preparation_recovers_without_rerunning_resolver(binary):
    return preparation_recovery(binary)


@requires(SSH, WORKER, PROCESS_EVENTS, command("ir"), command("uv"))
def test_discovery_recovers_without_capturing_another_context(binary):
    return preparation_recovery(binary, probe=True)


@requires(SSH, WORKER, PROCESS_EVENTS, command("ir"), command("uv"))
def test_completed_preparation_result_is_recovered_without_another_resolver(binary):
    with gated_session(binary, lease_ms=6000, faults=True, frame_gate=True) as (
        client,
        remote,
        started,
        release,
        exits,
        identities,
    ):
        client.send(r="value <- 42L; value", timeout_ms=0)
        owner = observe(remote, started, exits, identities)
        before = (remote / "identity").read_text()
        gate(remote.parent, "ssh-prepare", "down", "before", '"Completed":{"id":1')
        release.release()
        client.response_timeout = 180
        send_and_collect_runtime_python_resolution(client)
        assert "[1] 42\n" in last_result_text(client), last_result_text(client)
        assert not (remote.parent / "gates/ssh-prepare.action").exists(), (
            "completion loss checkpoint was not reached"
        )
        assert (remote / "identity").read_text() == before
        assert live_processes([owner]) == [owner[0]]
        return [
            {
                "completed_result_recovered": True,
                "resolver_repeated": False,
                "environment_committed": True,
            }
        ]


@requires(SSH, WORKER, PROCESS_EVENTS, command("ir"), command("uv"))
def test_preparation_input_blackhole_recovers(binary):
    return preparation_recovery(binary, direction="up")


@requires(SSH, WORKER, PROCESS_EVENTS, command("ir"), command("uv"))
def test_preparation_output_blackhole_recovers(binary):
    return preparation_recovery(binary, direction="down")


@requires(SSH, WORKER, PROCESS_EVENTS, command("ir"), command("uv"))
def test_preparation_bidirectional_blackhole_recovers(binary):
    return preparation_recovery(binary, direction="up down")


@requires(SSH, WORKER, PROCESS_EVENTS, command("ir"), command("uv"))
def test_both_channels_recover_during_restart_candidate_preparation(binary):
    with gated_session(binary, lease_ms=6000, faults=True) as (
        client,
        remote,
        started,
        release,
        exits,
        identities,
    ):
        client.send(r="value <- 41L", timeout_ms=0)
        observe(remote, started, exits, identities)
        release.release()
        client.response_timeout = 180
        send_and_collect_runtime_python_resolution(client)
        client.send(r="cat(Sys.getpid())")
        old_worker = capture_process_identity(int(last_result_text(client)))
        (remote / "claimed").unlink()
        pending = client.start_send(
            control="restart", r="42L", requirements={"r": ["praise"]}
        )
        owner = observe(remote, started, exits, identities)
        assert live_processes([old_worker]) == [old_worker[0]], (
            "candidate preparation retired the old worker"
        )
        fault(remote.parent / "sshd", "ssh-prepare", "close")
        fault(remote.parent / "sshd", "ssh-launch", "close")
        release.release()
        client.receive(pending)
        assert not pending["result"].get("isError"), pending
        assert "[1] 42\n" in last_result_text(client), last_result_text(client)
        assert live_processes([owner]) == [owner[0]]
        client.send(r="exists('value')")
        assert last_result_text(client) == "[1] FALSE\n", last_result_text(client)
        return [
            {
                "both_channels_recovered": True,
                "candidate_committed_before_replacement": True,
                "same_preparation_owner": True,
            }
        ]


@requires(SSH, WORKER, PROCESS_EVENTS, command("ir"), command("uv"))
def test_automatic_callback_result_recovers_in_the_existing_worker(binary):
    with gated_session(
        binary, lease_ms=6000, faults=True, frame_gate=True, isolated_libraries=True
    ) as (
        client,
        remote,
        started,
        release,
        exits,
        identities,
    ):
        client.send(r="value <- 41L", timeout_ms=0)
        owner = observe(remote, started, exits, identities)
        release.release()
        client.response_timeout = 180
        send_and_collect_runtime_python_resolution(client)
        (remote / "claimed").unlink()
        client.send(r="library(praise); value + 1L", timeout_ms=0)
        assert observe(remote, started, exits, identities) == owner
        resolver = (remote / "identity").read_text()
        gate(remote.parent, "ssh-prepare", "down", "before", '"Completed":')
        release.release()
        send_and_collect_runtime_python_resolution(client)
        assert last_result_text(client) == "[1] 42\n", last_result_text(client)
        assert (remote / "identity").read_text() == resolver
        assert not (remote.parent / "gates/ssh-prepare.action").exists()
        return [
            client.transcript[-1],
            {"same_preparation_owner": True, "callback_replayed": False},
        ]


if __name__ == "__main__":
    run_this_suite(__file__)
