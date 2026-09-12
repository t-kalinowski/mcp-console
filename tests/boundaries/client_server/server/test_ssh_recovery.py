#!/usr/bin/env -S uv run --script

import base64
import json
import os
import re
from pathlib import Path
import subprocess
import sys
from contextlib import closing, contextmanager
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.server.test_ssh_lease import fault
from support.assertions import last_result_text, wait_for_evaluation_output
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.normalization import code
from support.r import r_test_environment
from support.requirements import SANDBOX, WORKER, PROCESS_EVENTS, requires
from support.ssh import SSH, configure, localhost, remote_command
from support.suites import run_this_suite


@contextmanager
def recovery_session(binary, *, initial_loss=False, direct=False):
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        remote = root / "remote"
        remote.mkdir()
        environment, rscript = r_test_environment()
        libraries = subprocess.check_output(
            [
                rscript,
                "--vanilla",
                "-e",
                "cat(paste(.libPaths(), collapse=.Platform$path.sep))",
            ],
            env=environment,
            text=True,
        )
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
        gates = root / "gates"
        gates.mkdir()
        fixture = Path(__file__).resolve().parents[3] / "fixtures/ssh_frame_gate.py"
        prefix = [sys.executable, str(fixture), str(gates), *prefix]
        if initial_loss:
            (gates / "ssh-prepare.action").write_text(
                json.dumps({"direction": "down", "stage": "before", "tag": 1})
            )
        config = configure(
            root,
            remote,
            prefix,
            extends=":workspace",
            sandbox={
                "environment": {
                    "R_LIBS": libraries,
                    "RETICULATE_PYTHON": sys.executable,
                },
            },
        )
        target = json.loads(config.read_text())
        target["target"]["lease_ms"] = 6000
        config.write_text(json.dumps(target))
        with localhost(root / "sshd", faults=True) as environment:
            with McpClient(
                binary,
                ("serve", "--no-sandbox") if direct else ("serve",),
                environment,
                root,
            ) as client:
                client.initialize_and_list_tools()
                yield client, root, remote


@requires(SSH, WORKER, SANDBOX)
def test_connection_loss_preserves_worker_state_and_output(binary):
    with recovery_session(binary) as (client, root, remote):
        client.send(r="worker_pid <- Sys.getpid(); r_value <- 41L")
        client.send(python="import os; python_pid = os.getpid(); python_value = 42")
        with (
            closing(FifoCheckpoint.create(remote / "entered")) as entered,
            closing(FifoCheckpoint.create(remote / "release")) as release,
        ):
            client.send(
                python=code("""
                with open('entered', 'wb', buffering=0) as gate:
                    _ = gate.write(b'1')
                with open('release', 'rb', buffering=0) as gate:
                    assert gate.read(1) == b'1'
                with open('effects', 'a') as effects:
                    _ = effects.write('once\\n')
                print('output after interruption')
                """),
                timeout_ms=0,
            )
            entered.wait("worker accepted the cell before losing SSH")
            fault(root / "sshd", "ssh-launch", "close")
            release.release()
            client.request("ping")
            wait_for_evaluation_output(
                client, "output after interruption\n", "recovered cell"
            )
        assert (remote / "effects").read_text() == "once\n"
        client.send(r="stopifnot(Sys.getpid() == worker_pid); r_value")
        assert last_result_text(client) == "[1] 41\n", last_result_text(client)
        client.send(python="assert os.getpid() == python_pid; print(python_value)")
        assert last_result_text(client) == "42\n", last_result_text(client)
        plotted = client.send(r="plot(1:3)")
        images = [part for part in plotted["content"] if part["type"] == "image"]
        assert len(images) == 1, plotted
        session = next((root / ".agents/console/sessions").iterdir())
        artifacts = list((session / "artifacts").iterdir())
        assert len(artifacts) == 1
        assert artifacts[0].read_bytes() == base64.b64decode(images[0]["data"])
        return [
            {
                "same_worker_and_language_state": True,
                "cell_effect_count": 1,
                "output": "output after interruption\n",
                "local_image_count": 1,
            }
        ]


def gate(root, role, direction, stage, marker="", tag=2, **options):
    (root / "gates" / (role + ".action")).write_text(
        json.dumps(
            {
                "direction": direction,
                "stage": stage,
                "marker": marker,
                "tag": tag,
                **options,
            }
        )
    )


@requires(SSH, WORKER, SANDBOX)
def test_lost_initial_hello_attaches_to_the_same_owner(binary):
    with recovery_session(binary, initial_loss=True) as (client, root, remote):
        client.send(r="42L")
        assert last_result_text(client) == "[1] 42\n", last_result_text(client)
        return [{"lost_initial_reply_recovered": True}]


@requires(SSH, WORKER, SANDBOX)
def test_reconnect_keeps_its_setup_budget_within_the_live_lease(binary):
    with recovery_session(binary) as (client, root, remote):
        client.send(r="identity <- Sys.getpid(); value <- 42L")
        with (
            closing(FifoCheckpoint.create(root / "held")) as held,
            closing(FifoCheckpoint.create(root / "release")) as release,
            closing(FifoCheckpoint.create(root / "renewed")) as renewed,
            closing(FifoCheckpoint.create(remote / "evaluation")) as evaluation,
        ):
            client.send(
                r="connection <- fifo('evaluation', open='rb', blocking=TRUE); stopifnot(identical(readBin(connection, 'raw', 1L), as.raw(49))); close(connection); value",
                timeout_ms=0,
            )
            gate(
                root,
                "ssh-launch",
                "up",
                "hold",
                tag=1,
                checkpoint=str(held.path),
                release=str(release.path),
            )
            fault(root / "sshd", "ssh-launch", "close")
            held.wait("reconnect reached the paused bootstrap")
            attachment = (root / "gates/ssh-launch.attachment_pid").read_text()
            try:
                # Three challenges on the healthy channel span two complete
                # heartbeat intervals. They do not renew the paused channel.
                gate(
                    root,
                    "ssh-prepare",
                    "down",
                    "count",
                    tag=4,
                    remaining=3,
                    checkpoint=str(renewed.path),
                )
                renewed.wait("healthy preparation completed three challenges")
                assert (
                    root / "gates/ssh-launch.attachment_pid"
                ).read_text() == attachment, (
                    "reconnect discarded its setup attempt before the live lease expired"
                )
            finally:
                release.release()
                evaluation.release()
            wait_for_evaluation_output(client, "[1] 42\n", "recovered attachment")
            client.send(r="stopifnot(Sys.getpid() == identity); value")
            assert last_result_text(client) == "[1] 42\n", last_result_text(client)
            return [{"setup_attempt_preserved": True, "same_worker": True}]


def acceptance_loss(binary, stage):
    with recovery_session(binary) as (client, root, remote):
        client.send(r="identity <- Sys.getpid()")
        gate(root, "ssh-launch", "up", stage, "acceptance_marker")
        client.send(
            r='cat("acceptance_marker\\n", file="effects", append=TRUE); value <- 41L; value'
        )
        assert last_result_text(client) == "[1] 41\n", (
            last_result_text(client),
            client._diagnostics(),
        )
        assert (remote / "effects").read_text() == "acceptance_marker\n"
        client.send(r="stopifnot(identity == Sys.getpid()); value")
        assert last_result_text(client) == "[1] 41\n"
        return [{"loss": stage, "side_effect_count": 1, "same_worker": True}]


@requires(SSH, WORKER, SANDBOX)
def test_loss_before_command_acceptance_does_not_duplicate_evaluation(binary):
    return acceptance_loss(binary, "before")


@requires(SSH, WORKER, SANDBOX)
def test_loss_after_command_acceptance_does_not_duplicate_evaluation(binary):
    return acceptance_loss(binary, "accepted")


@requires(SSH, WORKER, SANDBOX)
def test_partial_image_frame_is_replayed_once_and_recorded_locally(binary):
    with recovery_session(binary) as (client, root, remote):
        client.send(r="identity <- Sys.getpid()")
        gate(root, "ssh-launch", "down", "partial", '"kind":"image"')
        result = client.send(r="plot(1:3)")
        images = [part for part in result["content"] if part["type"] == "image"]
        assert len(images) == 1, result
        session = next((root / ".agents/console/sessions").iterdir())
        artifacts = list((session / "artifacts").iterdir())
        assert len(artifacts) == 1
        assert artifacts[0].read_bytes() == base64.b64decode(images[0]["data"])
        events = [
            json.loads(line)
            for line in (session / "internal/events.jsonl").read_text().splitlines()
        ]
        assert sum(event["event"] == "artifact_created" for event in events) == 1
        image_results = [
            event
            for event in events
            if event["event"] == "tool_result"
            and any(part["type"] == "image" for part in event["result"]["content"])
        ]
        assert len(image_results) == 1
        assert not (root / "gates/ssh-launch.action").exists(), (
            "image checkpoint was not reached"
        )
        client.send(r="stopifnot(identity == Sys.getpid()); 42L")
        assert last_result_text(client) == "[1] 42\n"
        return [
            {
                "partial_frame_recovered": True,
                "delivered_images": 1,
                "recorded_images": 1,
            }
        ]


@requires(SSH, WORKER, SANDBOX)
def test_stdin_is_consumed_once_after_acceptance_loss(binary):
    with recovery_session(binary) as (client, root, remote):
        client.send(
            python="answer = input('first: '); print(answer); second = input('second: ')"
        )
        assert "[waiting for stdin]" in last_result_text(client)
        gate(root, "ssh-launch", "up", "accepted", "stdin_marker")
        wait_for_evaluation_output(
            client,
            'stdin_marker\n[input requested: "second: "]\n[waiting for stdin]',
            "recovered stdin delivery",
            stdin="stdin_marker\n",
        )
        wait_for_evaluation_output(
            client, "[done]", "second input completion", stdin="last value\n"
        )
        client.send(python="print(answer, second)")
        assert last_result_text(client) == "stdin_marker last value\n", (
            last_result_text(client)
        )
        return [{"stdin_effect_count": 1, "next_prompt_preserved": True}]


def blackhole_recovery(binary, direction):
    with recovery_session(binary) as (client, root, remote):
        client.send(r="identity <- Sys.getpid(); value <- 41L")
        fault(root / "sshd", "ssh-launch", direction)
        try:
            wait_for_evaluation_output(
                client,
                "[1] 42\n",
                "SSH blackhole recovery",
                completion_timeout_seconds=8,
                r="stopifnot(identity == Sys.getpid()); value <- value + 1L; value",
                timeout_ms=0,
            )
        except AssertionError as error:
            raise AssertionError((str(error), client._diagnostics())) from error
        client.send(r="value")
        assert last_result_text(client) == "[1] 42\n"
        return [
            {"blackholed_direction": direction, "same_worker": True, "effect_count": 1}
        ]


@requires(SSH, WORKER, SANDBOX)
def test_controller_to_worker_blackhole_recovers(binary):
    return blackhole_recovery(binary, "up")


@requires(SSH, WORKER, SANDBOX)
def test_worker_to_controller_blackhole_recovers(binary):
    return blackhole_recovery(binary, "down")


@requires(SSH, WORKER, SANDBOX)
def test_bidirectional_blackhole_recovers(binary):
    return blackhole_recovery(binary, "up down")


@requires(SSH, WORKER, SANDBOX)
def test_restart_recovers_terminal_receipt_before_replacement(binary):
    with recovery_session(binary) as (client, root, remote):
        client.send(r="value <- 41L")
        preparation = (root / "gates/ssh-prepare.owner").read_text()
        original = (root / "gates/ssh-launch.owner").read_text()
        gate(root, "ssh-launch", "up", "accepted", '"kind":"shutdown"')
        client.send(control="restart")
        assert not client.transcript[-1]["result"].get("isError"), client.transcript[-1]
        client.send(r="exists('value')")
        assert last_result_text(client) == "[1] FALSE\n", last_result_text(client)
        assert (root / "gates/ssh-launch.owner").read_text() != original
        assert (root / "gates/ssh-prepare.owner").read_text() == preparation
        return [
            {
                "retirement_receipt_recovered": True,
                "replacement_generation": True,
                "same_preparation_context": True,
            }
        ]


def interrupted_control(binary, stage):
    with recovery_session(binary) as (client, root, remote):
        with closing(FifoCheckpoint.create(remote / "entered")) as entered:
            client.send(
                r="con <- fifo('entered', open='wb', blocking=TRUE); writeBin(as.raw(49), con); close(con); repeat Sys.sleep(60)",
                timeout_ms=0,
            )
            entered.wait("R entered its interruptible cell")
            gate(root, "ssh-launch", "up", stage, '"kind":"interrupt"')
            wait_for_evaluation_output(
                client, "\n", "recovered interrupt", control="interrupt"
            )
        client.send(r="42L")
        assert last_result_text(client) == "[1] 42\n", last_result_text(client)
        return [{"interrupt_reconciled": True, "next_cell_unaffected": True}]


@requires(SSH, WORKER, SANDBOX)
def test_interrupt_after_acceptance_loss_keeps_its_generation(binary):
    return interrupted_control(binary, "accepted")


@requires(SSH, WORKER, SANDBOX)
def test_interrupt_queued_before_connection_loss_reaches_the_same_cell(binary):
    return interrupted_control(binary, "before")


@requires(SSH, WORKER, SANDBOX)
def test_sandbox_cannot_obtain_owner_capability_or_send_commands(binary):
    from support.processes import capture_process_identity, child_process_identities

    with recovery_session(binary) as (client, root, remote):
        client.send(r="value <- 41L")
        endpoint = (
            "/tmp/mcp-console-"
            + (root / "gates/ssh-launch.owner").read_text()
            + "/socket"
        )
        attachment = capture_process_identity(
            int((root / "gates/ssh-launch.attachment_pid").read_text())
        )
        owner = child_process_identities(attachment)[0]
        source = (
            code("""
            import ctypes, os, socket, sys
            assert not any(name in os.environ for name in ('MCP_CONSOLE_SSH_TARGET', 'MCP_CONSOLE_SSH_GENERATION'))
            if sys.platform == 'linux':
                try:
                    with open('/proc/' + str(OWNER) + '/mem', 'rb') as memory:
                        memory.read(1)
                except OSError:
                    pass
                else:
                    raise AssertionError('owner memory was readable')
            else:
                libc = ctypes.CDLL(None)
                task = ctypes.c_uint()
                assert libc.task_for_pid(libc.mach_task_self(), OWNER, ctypes.byref(task)) != 0
            probe = socket.socket(socket.AF_UNIX)
            probe.settimeout(3)
            try:
                probe.connect(ENDPOINT)
                probe.sendall(b'invalid attachment')
                assert not probe.recv(1)
            except OSError:
                pass
            finally:
                probe.close()
            print('owner memory and control remain private')
            """)
            .replace("OWNER", str(owner[0]))
            .replace("ENDPOINT", repr(endpoint))
        )
        client.send(python=source)
        assert (
            last_result_text(client) == "owner memory and control remain private\n"
        ), last_result_text(client)
        client.send(r="value")
        assert last_result_text(client) == "[1] 41\n", last_result_text(client)
        return [
            {
                "owner_memory_unreadable": True,
                "unauthenticated_control_rejected": True,
                "worker_state_preserved": True,
            }
        ]


@requires(SSH, WORKER)
def test_direct_retirement_does_not_wait_for_inherited_diagnostic_writers(binary):
    from support.processes import (
        capture_process_identity,
        kill_processes,
        live_processes,
    )

    identities = []
    try:
        with recovery_session(binary, direct=True) as (client, root, remote):
            with closing(FifoCheckpoint.create(remote / "descendant")) as checkpoint:
                client.send(
                    python=code("""
                    import subprocess, sys
                    child = subprocess.Popen([sys.executable, '-c', "with open('descendant', 'rb', buffering=0) as gate: gate.read(1)"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
                    print(child.pid)
                    """)
                )
                identities.append(
                    capture_process_identity(int(last_result_text(client).strip()))
                )
                client.send(control="restart")
                assert not client.transcript[-1]["result"].get("isError"), (
                    client.transcript[-1]
                )
                assert live_processes(identities), (
                    "direct execution unexpectedly claimed descendant cleanup"
                )
                checkpoint.release()
                return [
                    {
                        "retirement_bounded_by_owned_helper": True,
                        "direct_descendant_cleanup_guaranteed": False,
                    }
                ]
    finally:
        kill_processes(identities)


def controller_lifetime(binary, close_input=False):
    from support.events import Events
    from support.processes import (
        capture_process_identity,
        child_process_identities,
        host_process_id,
    )
    from support.ssh import ownership_ancestor
    import time

    with recovery_session(binary) as (client, root, remote), Events() as exits:
        client.send(r="cat(Sys.getpid(), Sys.getenv('TMPDIR'), sep='\\n')")
        pid, private = last_result_text(client).splitlines()
        worker = capture_process_identity(host_process_id(int(pid), os.getpid()))
        owners = [ownership_ancestor(worker)]
        attachment = capture_process_identity(
            int((root / "gates/ssh-prepare.attachment_pid").read_text())
        )
        owners.append(child_process_identities(attachment)[0])
        for identity in owners:
            exits.watch_process(identity[0])
        if close_input:
            fault(root / "sshd", "ssh-launch", "up down")
            fault(root / "sshd", "ssh-prepare", "up down")
            client.stdin.close()
        else:
            client.process.kill()
        client.process.wait(timeout=10)
        pending = {identity[0] for identity in owners}
        deadline = time.monotonic() + 9
        while pending:
            remaining = deadline - time.monotonic()
            assert remaining > 0
            pending.difference_update(exits.wait(remaining))
        assert not Path(private).exists()
        return [
            {
                "controller_died": not close_input,
                "both_remote_owners_retired": True,
                "sandbox_storage_removed": True,
            }
        ]


@requires(SSH, WORKER, SANDBOX, PROCESS_EVENTS)
def test_controller_death_retires_both_owners(binary):
    return controller_lifetime(binary)


@requires(SSH, WORKER, SANDBOX, PROCESS_EVENTS)
def test_input_closure_during_both_channel_interruptions_remains_bounded(binary):
    return controller_lifetime(binary, close_input=True)


@requires(SSH, WORKER, SANDBOX, PROCESS_EVENTS)
def test_missing_owner_does_not_replay_effects_or_clear_cleanup_uncertainty(binary):
    from support.events import Events
    from support.processes import capture_process_identity, host_process_id
    from support.ssh import ownership_ancestor
    import signal

    endpoint = None
    try:
        with recovery_session(binary) as (client, root, remote), Events() as exits:
            client.send(r="cat(Sys.getpid())")
            worker = capture_process_identity(
                host_process_id(int(last_result_text(client)), os.getpid())
            )
            owner = ownership_ancestor(worker)
            endpoint = Path(
                "/tmp/mcp-console-" + (root / "gates/ssh-launch.owner").read_text()
            )
            with closing(FifoCheckpoint.create(remote / "entered")) as entered:
                client.send(
                    r="cat('once\\n', file='effects'); con <- fifo('entered', open='wb', blocking=TRUE); writeBin(as.raw(49), con); close(con); Sys.sleep(60)",
                    timeout_ms=0,
                )
                entered.wait("side effect happened before ownership disappeared")
                exits.watch_process(worker[0])
                os.kill(owner[0], signal.SIGKILL)
                client.send()
                assert "unconfirmed" in last_result_text(client), last_result_text(
                    client
                )
                client.send(control="restart")
                assert "unconfirmed" in last_result_text(client), last_result_text(
                    client
                )
                assert worker[0] in exits.wait(9)
                assert (remote / "effects").read_text() == "once\n"
                return [
                    {
                        "owner_lost": True,
                        "effect_count": 1,
                        "replacement_blocked": True,
                        "cleanup_receipt_missing": True,
                    }
                ]
    finally:
        if endpoint:
            (endpoint / "socket").unlink(missing_ok=True)
            endpoint.rmdir()


@requires(SSH, WORKER, SANDBOX)
def test_terminal_output_recovers_while_local_ingestion_is_blocked(binary):
    import signal

    with recovery_session(binary) as (client, root, remote):
        with (
            closing(FifoCheckpoint.create(remote / "entered")) as entered,
            closing(FifoCheckpoint.create(remote / "release")) as release,
            closing(FifoCheckpoint.create(root / "cut")) as cut,
        ):
            client.send(
                r="entered <- fifo('entered', open='wb', blocking=TRUE); writeBin(as.raw(49), entered); close(entered); release <- fifo('release', open='rb', blocking=TRUE); readBin(release, 'raw', 1L); close(release); cat(strrep('x', 65536)); quit(save='no')",
                timeout_ms=0,
            )
            entered.wait("worker is ready to produce terminal output")
            (root / "gates/ssh-launch.action").write_text(
                json.dumps(
                    {
                        "direction": "down",
                        "stage": "after_end",
                        "tag": 6,
                        "checkpoint": str(cut.path),
                    }
                )
            )
            os.kill(client.process.pid, signal.SIGSTOP)
            assert os.WIFSTOPPED(os.waitpid(client.process.pid, os.WUNTRACED)[1])
            try:
                release.release()
                cut.wait(
                    "local adapter accepted terminal output before losing SSH",
                    timeout=8,
                )
            finally:
                os.kill(client.process.pid, signal.SIGCONT)
            first_output = len(client.transcript)
            client.send()
            output = "".join(
                item.get("text", "")
                for entry in client.transcript[first_output:]
                for item in entry.get("result", {}).get("content", [])
            )
            produced = sum(len(chunk) for chunk in re.findall("x{2,}", output))
            assert produced == 65536, (produced, output[:80], output[-200:])
            client.send(control="restart")
            assert not client.transcript[-1]["result"].get("isError"), (
                client.transcript[-1],
                client._diagnostics(),
            )
            return [
                {
                    "terminal_stream_recovered": True,
                    "local_backpressure_preserved": True,
                    "cleanup_receipt_recovered": True,
                }
            ]


if __name__ == "__main__":
    run_this_suite(__file__)
