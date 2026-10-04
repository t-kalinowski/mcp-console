"""Exercise broker validation and native failures through the resolve command."""

import json
import os
import select
import signal
import subprocess
import sys
import time
import tomllib
from contextlib import contextmanager, closing
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.checkpoints import FifoCheckpoint
from support.native import LOADER_VARIABLE, build_interposer
from support.processes import capture_process_identity, child_process_identities
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, SANDBOX, requires
from support.suites import run_this_suite


class Resolver:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self.buffer = b""

    def send(self, message: object) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()

    def receive(self, timeout: float = 10) -> object:
        assert self.process.stdout is not None
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            assert select.select(
                [self.process.stdout], [], [], max(0, deadline - time.monotonic())
            )[0], "resolver response timed out"
            chunk = os.read(self.process.stdout.fileno(), 65536)
            assert chunk, "resolver output closed"
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)


@contextmanager
def resolver(
    binary: Path,
    root: Path,
    broker_fault: str = "",
    workload_fault: str = "",
    *,
    sandboxed: bool = False,
):
    library = build_interposer(root, "resolver_faults")
    uv = root / "uv"
    uv.write_text(
        '#!/bin/sh\ncase "$1 $2" in\n\'python list\') printf \'%s\\n\' \'[{"version":"3.12.7","version_parts":{"major":3,"minor":12,"patch":7},"symlink":null,"variant":"default","implementation":"cpython"}]\';;\n\'tool run\') for last in "$@"; do :; done; printf \'%s\' "$MCP_CONSOLE_TEST_PYTHON" > "$last";;\n*) exit 90;;\nesac\n'
    )
    uv.chmod(0o755)
    destination = root / "cache/mcp-console/resolver/payload/started"
    environment = dict(
        os.environ,
        PATH=str(root),
        XDG_CACHE_HOME=str(root / "cache"),
        MCP_CONSOLE_TEST_PYTHON=sys.executable,
    )
    for name in ["RETICULATE_PYTHON", "RETICULATE_UV", "R_HOME"]:
        environment.pop(name, None)
    environment.update(
        {
            LOADER_VARIABLE: str(library),
            "MCP_CONSOLE_TEST_RESOLVER_FAULT": broker_fault,
            "MCP_CONSOLE_TEST_RESOLVER_DESTINATION": str(root / "policy.jsonl"),
        }
    )
    settings = {
        "environment": {
            LOADER_VARIABLE: str(library),
            "MCP_CONSOLE_TEST_RESOLVER_FAULT": workload_fault,
            "MCP_CONSOLE_TEST_RESOLVER_DESTINATION": str(destination),
        },
        "readable_roots": [str(root), str(Path(sys.executable).resolve().parents[1])],
    }
    build = tomllib.loads(
        (Path(__file__).resolve().parents[3] / "Cargo.toml").read_text()
    )["package"]["version"]
    with TemporaryFile() as diagnostics:
        process = subprocess.Popen(
            [binary, "resolve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=diagnostics,
            env=environment,
        )
        client = Resolver(process)
        try:
            client.send(
                {
                    "Open": {
                        "version": 6,
                        "build": build,
                        "workspace": "",
                        "selections": {"r_home": None, "python": None},
                        "mode": "PythonOnly",
                        "no_sandbox": not sandboxed,
                        "settings": settings,
                    }
                }
            )
            assert client.receive() == {"Hello": {"version": 6, "build": build}}
            discovery = client.receive()["Completed"]
            assert "Ok" in discovery["result"], discovery
            yield client, diagnostics, destination
        finally:
            if process.poll() is None:
                process.stdin.close()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


def prepare_python(client: Resolver) -> dict:
    client.send(
        {
            "Run": {
                "id": 1,
                "operation": {
                    "Python": {
                        "requirements": {
                            "packages": ["six>=1"],
                            "python_version": [],
                            "exclude_newer": None,
                        },
                        "r": None,
                    }
                },
            }
        }
    )
    return client.receive()["Completed"]


@requires(NATIVE_FIXTURES)
def test_direct_mode_validates_manifest_and_identity(binary: Path) -> Transcript:
    transcript = []
    for fault, expected in [
        ("manifest", "resolver changed the accepted Python manifest"),
        ("identity", "resolver returned inconsistent Python home"),
    ]:
        with TemporaryDirectory() as directory:
            with resolver(binary, Path(directory).resolve(), workload_fault=fault) as (
                client,
                _,
                _,
            ):
                result = prepare_python(client)
                assert expected in result["result"]["Err"], result
                assert result["confirmed"] is True, result
                client.send("Close")
                assert client.receive() == "Closed"
                transcript.append({"fault": fault, "rejected": True})
    return transcript


@requires(NATIVE_FIXTURES)
def test_diagnostic_interrupted_read_preserves_utf8(binary: Path) -> Transcript:
    for fault in ["eintr", "read-error"]:
        with TemporaryDirectory() as directory:
            with resolver(binary, Path(directory).resolve(), fault, "diagnostic") as (
                client,
                diagnostics,
                _,
            ):
                result = prepare_python(client)
                if fault == "eintr":
                    assert "Ok" in result["result"], result
                else:
                    assert (
                        "failed to read resolver stderr" in result["result"]["Err"]
                    ), result
                client.send("Close")
                assert client.receive() == "Closed"
                client.process.wait(timeout=10)
                diagnostics.seek(0)
                stderr = diagnostics.read().decode()
                if fault == "eintr":
                    assert "resolver diagnostic: α" in stderr and "�" not in stderr, (
                        stderr
                    )
    return [{"interrupted_read": "preserved UTF-8", "other_read_error": "propagated"}]


@requires(NATIVE_FIXTURES, SANDBOX)
def test_native_policy_reads_present_system_installations(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        with resolver(binary, root, "policy", sandboxed=True) as (client, _, _):
            client.send("Close")
            assert client.receive() == "Closed"
        policies = [
            json.loads(line)
            for line in (root / "policy.jsonl").read_text().splitlines()
        ]
        assert policies
        roots = [
            entry["path"]["path"]
            for entry in policies[0]["filesystem"]["entries"]
            if entry["access"] == "read"
        ]
        candidates = [
            "/etc/R",
            "/usr/local/bin",
            "/usr/local/opt",
            "/usr/local/lib",
            "/usr/local/Cellar",
        ]
        present = [path for path in candidates if Path(path).exists()]
        for path in present:
            assert path in roots, (path, roots)
        return [{"present_system_installations": "readable", "access": "read-only"}]


@requires(NATIVE_FIXTURES, SANDBOX)
def test_failed_forced_termination_bounds_open_stderr(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        with resolver(binary, root, "kill-error", "block", sandboxed=True) as (
            client,
            _,
            destination,
        ):
            with closing(FifoCheckpoint.create(destination)) as started:
                client.send(
                    {
                        "Run": {
                            "id": 1,
                            "operation": {
                                "InspectPython": {
                                    "executable": str(Path(sys.executable).resolve())
                                }
                            },
                        }
                    }
                )
                started.wait("native workload holds stdout and stderr open")
                launchers = child_process_identities(
                    capture_process_identity(client.process.pid)
                )
                assert len(launchers) == 1, launchers
                try:
                    client.send({"Control": {"id": 1, "control": "Cancelled"}})
                    assert client.receive()["Controlled"]["result"] == {"Ok": True}
                    result = client.receive(timeout=9)["Completed"]
                    assert result["confirmed"] is False, result
                    error = result["result"]["Err"]
                    assert "failed to force-stop runner" in error, error
                    assert "timed out collecting resolver diagnostics" in error, error
                    client.send("Close")
                    assert client.process.wait(timeout=3) != 0
                finally:
                    os.kill(launchers[0][0], signal.SIGTERM)
            metadata = json.loads(
                (root / "cache/mcp-console/resolver/control/metadata.json").read_text()
            )
            assert metadata["uncertain"] is True, metadata
    return [
        {
            "failed_forced_termination": "reported",
            "open_stderr": "bounded",
            "retirement": "unconfirmed",
            "storage": "quarantined",
        }
    ]


if __name__ == "__main__":
    run_this_suite(__file__)
