#!/usr/bin/env -S uv run --script

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.client import McpClient
from support.requirements import requires
from support.ssh import SSH, configure, localhost, poison_controller
from support.suites import run_this_suite


@requires(SSH)
def test_python_preparation_preserves_v3_peer_compatibility(binary):
    # Retain the R-present v3 payload shape; the peer negotiates v4 below.
    # The incompatible-peer case rejects v3 before MCP readiness.
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
                (python,) = [
                    r["operation"]["Python"]
                    for r in requests
                    if "Python" in r["operation"]
                ]
                assert set(python) == {"requirements", "r"}, python
                assert last_result_text(client) == "[prepared]", client.transcript[-1]
                declaration = client.send(requirements={"action": "get"})[
                    "structuredContent"
                ]["requirements"]
                assert "six" in declaration["python"], declaration
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
def test_incompatible_preparation_peer_fails_before_mcp_ready(binary):
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
                assert client.process.wait(timeout=12) != 0
                assert not client.stdout.read()
                errors = client.stderr.read()
                assert "incompatible SSH preparation" in errors, errors
                assert not trap.exists()
                return [{"stderr": errors}]


if __name__ == "__main__":
    run_this_suite(__file__)
