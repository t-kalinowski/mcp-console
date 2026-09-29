#!/usr/bin/env -S uv run --script

import json
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.requirements import requires
from support.ssh import SSH, configure, localhost, poison_controller
from support.suites import run_this_suite


@requires(SSH)
def test_discovery_outlives_connection_setup_timeout(binary):
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        record = root / "discovery"
        peer = Path(__file__).resolve().parents[3] / "fixtures/ssh_preparation_peer.py"
        configure(
            root, root, [sys.executable, str(peer), "delayed-discovery", str(record)]
        )
        with (
            closing(FifoCheckpoint.create(record.with_suffix(".started"))) as started,
            closing(FifoCheckpoint.create(record.with_suffix(".release"))) as release,
            localhost(root / "sshd") as environment,
        ):
            trap = poison_controller(root / "sshd", environment)
            with McpClient(
                binary, ("serve", "--no-sandbox"), environment, root
            ) as client:
                started.wait("peer sent the handshake and held discovery")
                client.initialize_and_list_tools()
                client.request("ping")
                try:
                    # Exercise the actual 30-second connection deadline while
                    # discovery is held at a checkpoint, without network delays.
                    client.process.wait(timeout=35)
                except subprocess.TimeoutExpired:
                    pass
                finally:
                    release.release()
                assert client.process.poll() is None, client.stderr.read()
                client.send(requirements={"action": "get"})
                assert not client.transcript[-1]["result"]["isError"]
                records = client.finish()[3:]
            assert not trap.exists()
            return records


@requires(SSH)
def test_python_preparation_preserves_v3_peer_compatibility(binary):
    # Retain the original R-present request shape without selected_python.
    # The peer negotiates the current preparation protocol independently.
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        record = root / "requests"
        peer = Path(__file__).resolve().parents[3] / "fixtures/ssh_preparation_peer.py"
        configure(root, root, [sys.executable, str(peer), "legacy-python", str(record)])
        with localhost(root / "sshd") as environment:
            trap = poison_controller(root / "sshd", environment)
            with McpClient(
                binary, ("serve", "--no-sandbox"), environment, root
            ) as client:
                client.initialize_and_list_tools()
                client.send(requirements={"python": ["six"]})
                requests = [
                    json.loads(line) for line in record.read_text().splitlines()
                ]
                python_requests = [
                    r["operation"]["Python"]
                    for r in requests
                    if "Python" in r["operation"]
                ]
                assert python_requests
                assert all(
                    set(request) == {"requirements", "r"} for request in python_requests
                ), python_requests
                assert any(
                    "six" in request["requirements"]["packages"]
                    for request in python_requests
                )
                inspections = [
                    r["operation"]["InspectPython"]
                    for r in requests
                    if "InspectPython" in r["operation"]
                ]
                assert inspections and all(
                    inspection == {"executable": "/remote-only/python"}
                    for inspection in inspections
                )
                assert last_result_text(client) == "[prepared]", client.transcript[-1]
                declaration = client.send(requirements={"action": "get"})[
                    "structuredContent"
                ]["requirements"]
                assert "six" in declaration["python"], declaration
                records = client.finish()[3:]
            assert not trap.exists()
            return records


@requires(SSH)
def test_initial_requirements_replace_a_launch_before_ready(binary):
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        record = root / "requests"
        peer = Path(__file__).resolve().parents[3] / "fixtures/ssh_preparation_peer.py"
        configure(
            root, root, [sys.executable, str(peer), "replace-before-ready", str(record)]
        )
        with (
            closing(FifoCheckpoint.create(record.with_suffix(".started"))) as started,
            localhost(root / "sshd") as environment,
        ):
            trap = poison_controller(root / "sshd", environment)
            with McpClient(
                binary, ("serve", "--no-sandbox"), environment, root
            ) as client:
                started.wait("automatic launch withheld Ready until shutdown")
                client.initialize_and_list_tools()
                client.send(requirements={"python": ["six"]})
                assert last_result_text(client) == "[prepared]", client.transcript[-1]
                declaration = client.send(requirements={"action": "get"})[
                    "structuredContent"
                ]["requirements"]
                assert "six" in declaration["python"], declaration
                assert record.with_suffix(".launches").read_text() == "launch\nlaunch\n"
                records = client.finish()[3:]
            assert not trap.exists()
            return records


@requires(SSH)
def test_invalid_preparation_results_never_commit_or_launch(binary):
    transcript = []
    for mode in (
        "unconfirmed",
        "truncated",
        "missing",
        "mismatched",
        "malformed",
        "truncated-result",
        "mismatched-chunk",
        "chunked-and-inline",
    ):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            record = root / "requests"
            peer = (
                Path(__file__).resolve().parents[3] / "fixtures/ssh_preparation_peer.py"
            )
            configure(root, root, [sys.executable, str(peer), mode, str(record)])
            with localhost(root / "sshd") as environment:
                trap = poison_controller(root / "sshd", environment)
                with McpClient(
                    binary, ("serve", "--no-sandbox"), environment, root
                ) as client:
                    client.initialize_and_list_tools()
                    client.send(requirements={"r": ["praise"]})
                    assert client.transcript[-1]["result"]["isError"], last_result_text(
                        client
                    )
                    original = record.read_text()
                    client.send(requirements={"r": ["praise"]})
                    client.send(control="restart", r="must_not_run <- TRUE")
                    assert client.transcript[-1]["result"]["isError"], last_result_text(
                        client
                    )
                    assert record.read_text() == original, (
                        "uncertain preparation was replayed"
                    )
                    assert not trap.exists()
                    client.stdin.close()
                    client.process.wait(timeout=12)
                    transcript.append({"peer": mode})
                    transcript.extend(client.transcript[3:])
                    transcript.append({"stderr": client.stderr.read()})
    return transcript


@requires(SSH)
def test_default_extension_failure_closes_preparation(
    binary: Path,
) -> list[dict[str, str]]:
    transcript = []
    for mode in ("default-extension-failure", "default-extension-close-failure"):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            record = root / "requests"
            peer = (
                Path(__file__).resolve().parents[3] / "fixtures/ssh_preparation_peer.py"
            )
            configure(root, root, [sys.executable, str(peer), mode, str(record)])
            with localhost(root / "sshd") as environment:
                trap = poison_controller(root / "sshd", environment)
                with McpClient(
                    binary, ("serve", "--no-sandbox"), environment, root
                ) as client:
                    client.initialize_and_list_tools()
                    client.send(python="42")
                    assert client.transcript[-1]["result"]["isError"]
                    errors = last_result_text(client)
                    expected = "SQLite preparation failed"
                    if mode == "default-extension-close-failure":
                        # The close error proves startup waited for the peer's reply.
                        expected += "; unexpected SSH preparation event"
                    assert errors == "[" + expected + "]", errors
                    client.request("ping")
                    client.finish()
                    assert record.with_suffix(".closed").exists()
                    assert not trap.exists()
                    transcript.append({"peer": mode, "stderr": errors})
    return transcript


@requires(SSH)
def test_incompatible_preparation_peer_preserves_mcp_readiness(binary):
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        peer = Path(__file__).resolve().parents[3] / "fixtures/ssh_preparation_peer.py"
        configure(
            root,
            root,
            [sys.executable, str(peer), "incompatible", str(root / "requests")],
        )
        with localhost(root / "sshd") as environment:
            trap = poison_controller(root / "sshd", environment)
            with McpClient(
                binary, ("serve", "--no-sandbox"), environment, root
            ) as client:
                client.initialize_and_list_tools()
                client.send(r="42")
                assert client.transcript[-1]["result"]["isError"]
                errors = last_result_text(client)
                assert "incompatible SSH preparation" in errors, errors
                client.request("ping")
                client.finish()
                assert not trap.exists()
                return [{"stderr": errors}]


if __name__ == "__main__":
    run_this_suite(__file__)
