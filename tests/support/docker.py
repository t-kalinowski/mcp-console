"""Real Docker image fixture and daemon capability, shared by public cases."""

import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from support.requirements import Requirement

ROOT = Path(__file__).resolve().parents[2]


def daemon_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info", "--format", "{{.OSType}}"],
                capture_output=True,
                timeout=5,
            ).stdout.strip()
            == b"linux"
        )
    except subprocess.TimeoutExpired:
        return False


DOCKER = Requirement(
    "Linux Docker image fixture",
    bool(os.environ.get("MCP_CONSOLE_TEST_DOCKER_IMAGE")) and daemon_available(),
    "requires a Linux Docker daemon and MCP_CONSOLE_TEST_DOCKER_IMAGE built from examples/docker/Dockerfile",
)


def docker(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=kwargs.pop("timeout", 30),
        **kwargs,
    )


def image() -> str:
    selected = os.environ["MCP_CONSOLE_TEST_DOCKER_IMAGE"]
    result = docker("image", "inspect", "--format", "{{.Id}}", selected)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@contextmanager
def tagged_image(reference: str):
    # BuildKit FROM accepts image references, not bare local sha256 IDs.
    tag = "mcp-console-build-fixture:" + uuid4().hex
    result = docker("image", "tag", reference, tag)
    assert result.returncode == 0, result.stderr
    try:
        yield tag
    finally:
        result = docker("image", "rm", tag)
        assert result.returncode == 0, result.stderr


@contextmanager
def workspace():
    directory = ROOT / "target/docker-tests"
    directory.mkdir(parents=True, exist_ok=True)
    # Controller paths are inside the project so Docker Desktop/Colima's normal
    # home-directory sharing can supply these explicit binds.
    with TemporaryDirectory(dir=directory, prefix="case ") as temporary:
        yield Path(temporary).resolve()


def configure(
    root: Path, reference: str, *, mounts=None, environment=None, **target
) -> Path:
    config = root / ".agents/console/config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps(
            {
                "target": {
                    "transport": {"kind": "local"},
                    "workspace": "/workspace",
                    "command": ["mcp-console"],
                    "compute": {
                        "kind": "docker",
                        "image": reference,
                        "pull": "never",
                        "mounts": mounts or [],
                    },
                    **target,
                },
                "sandbox": {
                    "filesystem": {"kind": "external-sandbox"},
                    "network": "enabled",
                    "environment": environment or {},
                },
            }
        )
    )
    return config


def absent(container: str) -> None:
    selector = (
        f"name=^/{container}$"
        if container.startswith("mcp-console-")
        else f"id={container}"
    )
    result = docker("container", "ls", "--all", "--quiet", "--filter", selector)
    assert result.returncode == 0 and not result.stdout.strip(), result


def native_sandbox_available(reference: str) -> bool:
    """Probe the image's actual native boundary under ordinary daemon settings."""
    created = docker(
        "create",
        "--init",
        "--entrypoint",
        "mcp-console",
        reference,
        "sandbox",
        "--",
        "/usr/bin/true",
    )
    assert created.returncode == 0, created.stderr
    identity = created.stdout.strip()
    try:
        probe = docker("start", "--attach", identity, timeout=15)
        return probe.returncode == 0
    finally:
        removed = docker("rm", "--force", "--volumes", identity)
        assert removed.returncode == 0, removed.stderr


def cli_peer(root: Path) -> dict[str, str]:
    root.mkdir()
    wrapper = root / "docker"
    fixture = ROOT / "tests/fixtures/docker_cli.py"
    wrapper.write_text(f"#!{sys.executable}\n" + fixture.read_text())
    wrapper.chmod(0o755)
    return {
        **os.environ,
        "PATH": str(root) + os.pathsep + os.environ["PATH"],
        "CONSOLE_DOCKER_PEER": str(root),
        "CONSOLE_REAL_DOCKER": shutil.which("docker") or "/unused/docker",
    }


def normalize_recording(records: list, root: Path) -> list:
    """Keep complete output, replacing only identities observed by this case."""
    text = json.dumps(records)
    calls = root / "peer/calls"
    if calls.exists():
        for line in calls.read_text().splitlines():
            args = json.loads(line)["args"]
            if "create" in args:
                text = text.replace(args[args.index("--name") + 1], "<owned container>")
            if "start" in args:
                identity = args[-1]
                text = text.replace(identity, "<container>")
                text = text.replace(identity[:12], "<container>")
    return json.loads(text.replace(str(root), "<docker-test>"))


@contextmanager
def removal_event(container: str):
    from support.client import TextReader

    process = subprocess.Popen(
        [
            "docker",
            "events",
            "--since",
            str(time.time()),
            "--filter",
            f"container={container}",
            "--filter",
            "event=destroy",
            "--format",
            "{{json .}}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    reader = TextReader(process.stdout)
    try:
        yield lambda: json.loads(reader.readline(timeout=15))
    finally:
        process.terminate()
        process.wait(timeout=5)
        reader.close()
