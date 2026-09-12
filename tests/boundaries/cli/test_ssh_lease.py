#!/usr/bin/env -S uv run --script

import os
import json
from pathlib import Path
import signal
import struct
import subprocess
import sys
import threading
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.events import Events
from support.processes import (
    capture_process_identity,
    child_process_identities,
    kill_processes,
)
from support.requirements import POSIX, PROCESS_EVENTS, requires
from support.ssh_protocol import Owner, ZERO, encoded, frame
from support.suites import run_this_suite


def owner_identity(owner):
    return child_process_identities(capture_process_identity(owner.processes[0].pid))[0]


def wait_retirement(owner, exits, identity):
    assert identity[0] in exits.wait(10), "remote owner did not retire"
    assert not owner.directory.exists()


@requires(POSIX)
def test_incompatible_lease_fails_before_discovery(binary):
    records = []
    for override in ({"version": 999}, {"build": "other-build"}, {"lease_ms": 0}):
        with closing(Owner(binary)) as owner:
            request = {
                "hello": {**owner.hello, **override},
                "epoch": 1,
                "nonce": [0] * 32,
                "create": True,
                "secret": list(owner.secret),
            }
            result = subprocess.run(
                [binary, "ssh-tunnel", "ssh-prepare"],
                input=frame(1, encoded(request)),
                capture_output=True,
                timeout=10,
            )
            assert result.returncode != 0 and result.stdout[0] == 10, result
            assert not owner.directory.exists()
            records.append({"input": override, "stderr": result.stderr.decode()})
    return records


@requires(POSIX, PROCESS_EVENTS)
def test_partial_frame_cannot_delay_remote_expiry(binary):
    with closing(Owner(binary)) as owner, Events() as exits:
        wire = owner.attach()
        owner.started(wire)
        identity = owner_identity(owner)
        exits.watch_process(identity[0])
        wire.destination.write(struct.pack(">BI", 2, 100) + b"partial")
        wire.destination.flush()
        wait_retirement(owner, exits, identity)
        return [{"partial_frame_still_open": True, "remote_owner_retired": True}]


@requires(POSIX, PROCESS_EVENTS)
def test_repeated_response_does_not_renew_lease(binary):
    with closing(Owner(binary)) as owner, Events() as exits:
        wire = owner.attach()
        identity = owner_identity(owner)
        exits.watch_process(identity[0])
        tag, token = owner.receive(wire, respond=False)
        assert tag == 4
        wire.send(5, token)
        wire.send(5, token)
        wait_retirement(owner, exits, identity)
        return [{"repeated_response_rejected": True, "remote_owner_retired": True}]


@requires(POSIX, PROCESS_EVENTS)
def test_shutdown_deadline_is_independent_of_healthy_heartbeats(binary):
    children = []
    try:
        with closing(Owner(binary, lease_ms=30000)) as owner, Events() as exits:
            wire = owner.attach()
            owner.started(wire)
            identity = owner_identity(owner)
            children = child_process_identities(identity)
            assert len(children) == 1
            exits.watch_process(identity[0])
            os.kill(children[0][0], signal.SIGSTOP)
            wire.send(6, encoded(ZERO))

            def renew():
                try:
                    while True:
                        owner.receive(wire)
                except (OSError, AssertionError):
                    pass

            reader = threading.Thread(target=renew, daemon=True)
            reader.start()
            wait_retirement(owner, exits, identity)
            reader.join(timeout=1)
            assert not reader.is_alive()
            return [
                {
                    "healthy_heartbeats_cannot_extend_shutdown": True,
                    "remote_owner_retired": True,
                }
            ]
    finally:
        kill_processes(children)


@requires(POSIX, PROCESS_EVENTS)
def test_failed_and_stale_attachments_do_not_displace_current_owner(binary):
    with closing(Owner(binary, lease_ms=6000)) as owner, Events() as exits:
        wire = owner.attach()
        owner.started(wire)
        identity = owner_identity(owner)
        exits.watch_process(identity[0])
        for options in (
            {"secret": b"wrong capability".ljust(32, b"!"), "epoch": 2},
            {"epoch": 1},
        ):
            try:
                owner.attach(**options)
            except AssertionError:
                owner.processes[-1].stdin.close()
                owner.processes[-1].wait(timeout=3)
            else:
                raise AssertionError("invalid attachment was accepted")
            while owner.receive(wire)[0] != 4:
                pass
        replacement = owner.attach(epoch=3)
        while owner.receive(replacement)[0] != 4:
            pass
        assert owner.processes[0].wait(timeout=3) == 0
        replacement.send(6, encoded(owner.tx))
        try:
            while True:
                owner.receive(replacement)
        except AssertionError:
            pass
        wait_retirement(owner, exits, identity)
        return [
            {
                "bad_credentials_rejected": True,
                "stale_epoch_rejected": True,
                "higher_epoch_fenced_old_stream": True,
            }
        ]


@requires(POSIX, PROCESS_EVENTS)
def test_expired_owner_cannot_be_recreated_by_attachment(binary):
    with closing(Owner(binary)) as owner, Events() as exits:
        wire = owner.attach()
        identity = owner_identity(owner)
        exits.watch_process(identity[0])
        # Authentication alone does not renew the lease or start discovery.
        assert not child_process_identities(identity)
        wait_retirement(owner, exits, identity)
        try:
            owner.attach()
        except AssertionError as error:
            assert "missing or unavailable" in str(error), error
        else:
            raise AssertionError("expired owner was recreated")
        assert not owner.directory.exists()
        return [
            {
                "authentication_did_not_start_work": True,
                "expired_owner_recreated": False,
            }
        ]


@requires(POSIX, PROCESS_EVENTS)
def test_simultaneous_authenticated_attachments_install_one_epoch(binary):
    from concurrent.futures import ThreadPoolExecutor

    with closing(Owner(binary, lease_ms=6000)) as owner, Events() as exits:
        initial = owner.attach()
        owner.started(initial)
        identity = owner_identity(owner)
        exits.watch_process(identity[0])
        with ThreadPoolExecutor(2) as executor:
            attempts = [executor.submit(owner.attach, epoch=2) for _ in range(2)]
            accepted = []
            for attempt in attempts:
                try:
                    accepted.append(attempt.result())
                except AssertionError:
                    pass
        assert len(accepted) == 1, len(accepted)
        while owner.receive(accepted[0])[0] != 4:
            pass
        accepted[0].send(6, encoded(owner.tx))
        try:
            while True:
                owner.receive(accepted[0])
        except AssertionError:
            pass
        wait_retirement(owner, exits, identity)
        return [{"one_current_attachment": True}]


@requires(POSIX, PROCESS_EVENTS)
def test_packet_identity_cannot_be_reused_with_different_content(binary):
    with closing(Owner(binary, lease_ms=6000)) as owner, Events() as exits:
        wire = owner.attach()
        identity = owner_identity(owner)
        exits.watch_process(identity[0])
        # No normal challenge response yet: the helper cannot consume bytes.
        body = owner.data(wire, b"original")
        wire.send(2, body[:-1] + b"X")
        while True:
            tag, message = owner.receive(wire, respond=False)
            if tag == 10:
                assert b"identity reused" in message, message
                break
        wait_retirement(owner, exits, identity)
        return [{"changed_content_rejected": True, "work_started": False}]


@requires(POSIX, PROCESS_EVENTS)
def test_exhausted_replay_capacity_fails_without_dispatch(binary):
    with closing(Owner(binary, lease_ms=6000)) as owner, Events() as exits:
        wire = owner.attach()
        identity = owner_identity(owner)
        exits.watch_process(identity[0])
        for _ in range(17):
            owner.data(wire, b"x" * 16384)
        while True:
            tag, message = owner.receive(wire, respond=False)
            if tag == 10:
                assert b"capacity exhausted" in message, message
                break
        wait_retirement(owner, exits, identity)
        return [
            {
                "bounded_replay_capacity": True,
                "overflow_rejected": True,
                "work_started": False,
            }
        ]


@requires(POSIX, PROCESS_EVENTS)
def test_old_authenticated_frame_cannot_act_on_a_new_epoch(binary):
    with closing(Owner(binary, lease_ms=6000)) as owner, Events() as exits:
        old = owner.attach()
        owner.started(old)
        identity = owner_identity(owner)
        exits.watch_process(identity[0])
        stale = old.encode(6, encoded(ZERO))
        current = owner.attach()
        current.destination.write(stale)
        current.destination.flush()
        while True:
            tag, message = owner.receive(current, respond=False)
            if tag == 10:
                assert b"authentication failed" in message, message
                break
        wait_retirement(owner, exits, identity)
        return [{"old_epoch_frame_rejected": True}]


@requires(POSIX, PROCESS_EVENTS)
def test_shutdown_before_first_renewal_does_not_start_a_helper(binary):
    with closing(Owner(binary, lease_ms=30000)) as owner, Events() as exits:
        wire = owner.attach()
        identity = owner_identity(owner)
        exits.watch_process(identity[0])
        tag, token = owner.receive(wire, respond=False)
        assert tag == 4
        wire.send(6, encoded(ZERO))
        wire.send(5, token)
        while True:
            tag, body = wire.receive(timeout=2)
            if tag == 6:
                assert not child_process_identities(identity), (
                    "shutdown started a new helper"
                )
                wire.send(7, encoded(ZERO))
                break
            assert tag == 7, (tag, body)
        wait_retirement(owner, exits, identity)
        return [{"shutdown_before_liveness_started_no_helper": True}]


@requires(POSIX, PROCESS_EVENTS)
def test_late_input_preserves_an_exited_helpers_terminal_receipt(binary):
    with (
        closing(Owner(binary, lease_ms=6000, operation="ssh-launch")) as owner,
        Events() as exits,
    ):
        wire = owner.attach()
        owner.started(wire)
        identity = owner_identity(owner)
        helper = child_process_identities(identity)[0]
        exits.watch_process(helper[0])
        exits.watch_process(identity[0])
        # A rejected bootstrap emits a confirmed terminal result without
        # starting a workload. Observe helper exit before sending late input.
        owner.data(wire, struct.pack(">I", 2) + b"{}")
        assert helper[0] in exits.wait(3)
        accepted = owner.tx.copy()
        owner.data(wire, b"late controller input")
        terminal = bytearray()
        challenges = 0
        end = None
        while challenges < 2:
            tag, body = owner.receive(wire)
            assert tag != 10, body.decode()
            if tag == 2 and body[40] == 0:
                terminal.extend(body[41:])
            elif tag == 3:
                assert json.loads(body)["id"] <= accepted["id"]
            elif tag == 4:
                challenges += 1
            elif tag == 6:
                end = body
        assert end is not None
        assert terminal[0] == 3
        result = json.loads(terminal[5:])
        assert result["confirmed"] is True, result
        wire.send(7, end)
        wait_retirement(owner, exits, identity)
        return [{"terminal_receipt_preserved": True, "late_input_dispatched": False}]


if __name__ == "__main__":
    run_this_suite(__file__)
