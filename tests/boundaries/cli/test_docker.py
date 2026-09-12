#!/usr/bin/env -S uv run --script

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.records import Transcript
from support.client import McpClient
from support.requirements import POSIX, requires
from support.suites import run_this_suite


def test_invalid_docker_configuration(binary: Path) -> Transcript:
    cases = (
        ({"image": "example", "pull": "sometimes"}, "sometimes"),
        (
            {"image": "example", "build": {"context": ".", "dockerfile": "Dockerfile"}},
            "mutually exclusive",
        ),
        ({"build": {"context": "."}}, "dockerfile"),
        (
            {"build": {"context": ".", "dockerfile": "Dockerfile"}, "pull": "never"},
            "pull",
        ),
        ({}, "image or build"),
        (
            {"image": "example", "mounts": [{"source": ".", "target": "relative"}]},
            "absolute",
        ),
        ({"image": "example", "privileged": True}, "privileged"),
    )
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        for compute, expected in cases:
            config.write_text(
                json.dumps(
                    {
                        "target": {
                            "workspace": "/workspace",
                            "compute": {"kind": "docker", **compute},
                        }
                    }
                )
            )
            result = subprocess.run(
                [binary, "serve"],
                cwd=root,
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode != 0 and not result.stdout, result
            assert expected in result.stderr, result.stderr
            records.append({"compute": compute, "error": result.stderr})
    return records


@requires(POSIX)
def test_explicit_local_host_uses_the_existing_launch_path(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        baseline = None
        for target in (
            None,
            {},
            {"transport": {"kind": "local"}},
            {"compute": {"kind": "host"}},
            {"transport": {"kind": "local"}, "compute": {"kind": "host"}},
        ):
            config.write_text(json.dumps({} if target is None else {"target": target}))
            with McpClient(
                binary,
                ("serve", "--no-sandbox", "--worker", "/usr/bin/true"),
                current_directory=root,
            ) as client:
                client.initialize_and_list_tools()
                tools = client.transcript[-1]["result"]["tools"]
                if baseline is None:
                    baseline = tools
                assert tools == baseline
                client.finish()
        return [{"omitted_and_explicit_local_host_targets_are_equivalent": True}]


def test_unsupported_target_combinations(binary: Path) -> Transcript:
    cases = [
        {"compute": {"kind": "docker", "image": "example"}},
        {
            "workspace": "/workspace",
            "transport": {"kind": "ssh", "host": "example"},
            "compute": {"kind": "docker", "image": "example"},
        },
        {
            "workspace": "/workspace",
            "compute": {
                "kind": "docker",
                "image": "example",
                "mounts": [{"target": "/workspace"}],
            },
        },
        {
            "workspace": "/workspace",
            "compute": {
                "kind": "docker",
                "image": "example",
                "mounts": [{"source": ".", "target": "/workspace", "access": "write"}],
            },
        },
        {
            "workspace": "/workspace",
            "command": [],
            "compute": {"kind": "docker", "image": "example"},
        },
        {"transport": {"kind": "local", "host": "example"}},
        {"compute": {"kind": "host", "image": "example"}},
    ]
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        for target in cases:
            config.write_text(json.dumps({"target": target}))
            result = subprocess.run(
                [binary, "serve"],
                cwd=root,
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode != 0 and not result.stdout, result
            assert ".agents/console/config.yaml:" in result.stderr, result.stderr
            records.append({"target": target, "stderr": result.stderr})
    return records


if __name__ == "__main__":
    run_this_suite(__file__)
