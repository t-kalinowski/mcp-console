"""Standalone sbx capability and explicit fixtures, independent of Docker Engine."""

import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from support.client import McpClient
from support.capture import read_jsonl_path
from support.requirements import WORKER, Requirement

ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def runtime_lock():
    directory = ROOT / "target/sbx-tests"
    directory.mkdir(parents=True, exist_ok=True)
    # Share this lock with capability discovery: concurrent ls/policy requests
    # during VM creation can otherwise disagree between discovery and execution.
    import fcntl

    with (directory / "runtime.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield directory


def sbx(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sbx", *args],
        capture_output=True,
        text=True,
        timeout=kwargs.pop("timeout", 30),
        **kwargs,
    )


def available() -> bool:
    if (
        not WORKER.available
        or not os.environ.get("MCP_CONSOLE_TEST_SBX_TEMPLATE")
        or not shutil.which("sbx")
    ):
        return False
    try:
        with runtime_lock():
            return (
                sbx("version").stdout.startswith("sbx version: v0.42.1 ")
                and sbx("ls", "--json", timeout=5).returncode == 0
            )
    except subprocess.TimeoutExpired:
        return False


DOCKER_SANDBOX = Requirement(
    "Docker Sandbox fixture",
    available(),
    "requires standalone sbx v0.42.1, usable local virtualization/login/policy, and MCP_CONSOLE_TEST_SBX_TEMPLATE built from examples/docker-sandbox/Dockerfile",
)


def network_fixture() -> bool:
    if (
        not DOCKER_SANDBOX.available
        or os.environ.get("MCP_CONSOLE_TEST_SBX_NETWORK") != "1"
    ):
        return False
    with runtime_lock():
        return all(
            json.loads(sbx("policy", "check", "network", "--json", host).stdout)[
                "allowed"
            ]
            == allowed
            for host, allowed in (
                ("pypi.org:443", True),
                ("registry.npmjs.org:443", True),
                ("example.com:443", False),
            )
        )


DOCKER_SANDBOX_NETWORK = Requirement(
    "Docker Sandbox inherited network policy",
    network_fixture(),
    "requires MCP_CONSOLE_TEST_SBX_NETWORK=1 and an existing policy allowing pypi.org/registry.npmjs.org:443 and denying example.com:443; tests never initialize or reset global policy",
)

DOCKER_SANDBOX_INNER_DOCKER = Requirement(
    "Docker Sandbox inner Docker fixture",
    DOCKER_SANDBOX.available
    and os.environ.get("MCP_CONSOLE_TEST_SBX_INNER_DOCKER") == "1",
    "requires MCP_CONSOLE_TEST_SBX_INNER_DOCKER=1, the example shell-docker template, and existing provider policy permitting a busybox:1.37.0 pull inside the owned VM",
)


@contextmanager
def workspace(*, real: bool = False):
    directory = ROOT / "target/sbx-tests"
    directory.mkdir(parents=True, exist_ok=True)
    # sbx defaults each VM to half the controller's memory (up to 32 GiB).
    # CPU-count transcript concurrency can overcommit the shared VM runtime.
    # Serialize real fixtures; fake CLI tests retain ordinary concurrency.
    from contextlib import nullcontext

    with runtime_lock() if real else nullcontext():
        with TemporaryDirectory(dir=directory, prefix="case ") as temporary:
            yield Path(temporary).resolve()


def configure(
    root: Path, *, template: str | None = None, mounts=None, environment=None, **target
) -> Path:
    config = root / ".agents/console/config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps(
            {
                "target": {
                    "workspace": "/workspace",
                    "compute": {
                        "kind": "docker_sandbox",
                        "template": template
                        or os.environ["MCP_CONSOLE_TEST_SBX_TEMPLATE"],
                        "mounts": mounts or [],
                    },
                    **target,
                },
                "sandbox": {"provider": "compute", "environment": environment or {}},
            }
        )
    )
    return config


def cli_peer(root: Path, *, real: bool = False) -> dict[str, str]:
    root.mkdir()
    wrapper = root / "sbx"
    wrapper.write_text(
        f"#!{sys.executable}\n" + (ROOT / "tests/fixtures/sbx_cli.py").read_text()
    )
    wrapper.chmod(0o755)
    return {
        **os.environ,
        "PATH": str(root) + os.pathsep + os.environ["PATH"],
        "CONSOLE_SBX_PEER": str(root),
        **({"CONSOLE_SBX_REAL": shutil.which("sbx")} if real else {}),
    }


def isolated_controller(
    binary: Path, root: Path, *, real: bool = False
) -> tuple[Path, dict[str, str]]:
    """Relocate Console without its companion and expose forbidden host calls."""
    prefix = root / "installation"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "libexec").mkdir()
    relocated = prefix / "bin/mcp-console"
    shutil.copyfile(binary, relocated)
    relocated.chmod(0o755)
    environment = cli_peer(root / "peer", real=real)
    paths = [prefix / "libexec/mcp-console-sandbox"]
    paths.extend(
        root / "peer" / name
        for name in ("mcp-console-sandbox", "R", "Rscript", "uv", "ir")
    )
    for path in paths:
        path.write_text(f"""#!/bin/sh
echo invoked >> '{root / "sentinel"}'
exit 99
""")
        path.chmod(0o755)
    return relocated, environment


def calls(root: Path) -> list[dict]:
    return read_jsonl_path(root / "peer/calls")


def absent(name: str, id: str) -> None:
    result = sbx("ls", "--json")
    assert result.returncode == 0, result.stderr
    assert not any(
        vm["name"] == name or vm["id"] == id
        for vm in json.loads(result.stdout)["sandboxes"]
    ), result.stdout


def generations(root: Path) -> list[dict]:
    journal = next(root.glob(".agents/console/sessions/*/internal/events.jsonl"))
    return [
        event["sandbox"]
        for line in journal.read_text().splitlines()
        if (event := json.loads(line))["event"] == "target_generation"
    ]


def normalize_recording(recording: list, root: Path) -> list:
    value = json.dumps(recording).replace(str(root), "<sandbox-test>")
    value = re.sub(r"mcp-console-[0-9a-f]{32}", "<owned-microvm>", value)
    value = re.sub(
        r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", "<microvm-id>", value
    )
    return json.loads(value)


def finish(client: McpClient, root: Path) -> list:
    # Successful sbx removal reports progress. Preserve provider diagnostics
    # separately from MCP records instead of asserting an empty stderr pipe.
    recording, diagnostics = client.finish_with_standard_error()
    return normalize_recording(recording + [{"standard_error": diagnostics}], root)
