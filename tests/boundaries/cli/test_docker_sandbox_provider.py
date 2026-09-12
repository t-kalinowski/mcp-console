#!/usr/bin/env -S uv run --script
"""Public configuration rejects unsupported provider requests before setup."""

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.records import Transcript
from support.suites import run_this_suite

TEMPLATE = "docker.io/example/console@sha256:" + "a" * 64


def test_compute_provider_rejects_native_policy(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        target = {
            "workspace": "/workspace",
            "compute": {"kind": "docker_sandbox", "template": TEMPLATE},
        }
        for field in (
            "filesystem",
            "network",
            "proxy",
            "linux_backend",
            "workspace_options",
            "macos_seatbelt_profile_extension",
            "lifecycle",
            "version",
            "workspace",
            "extends",
        ):
            config.write_text(
                json.dumps(
                    {"target": target, "sandbox": {"provider": "compute", field: {}}}
                )
            )
            for flags in ([], ["--no-sandbox"]):
                result = subprocess.run(
                    [binary, "serve", *flags],
                    cwd=root,
                    input="",
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert result.returncode != 0 and not result.stdout, result
                assert (
                    f"sandbox.{field}" in result.stderr
                    and "Docker Sandbox" in result.stderr
                ), result.stderr
                records.append({"field": field, "flags": flags, "error": result.stderr})
        for extra, flags, expected in (
            ({"extends": ":workspace"}, [], "extends"),
            ({"extends": None}, [], "extends"),
            ({}, ["--writable-root", "/workspace"], "--writable-root"),
        ):
            config.write_text(json.dumps({"target": target, **extra}))
            result = subprocess.run(
                [binary, "serve", *flags],
                cwd=root,
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert (
                result.returncode != 0
                and expected in result.stderr
                and "Docker Sandbox" in result.stderr
            ), result
            records.append({"field": expected, "error": result.stderr})
    return records


def test_standalone_rejects_resolved_compute_provider(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        records = []
        for sandbox in ({}, {"provider": "compute"}):
            config.write_text(
                json.dumps(
                    {
                        "target": {
                            "workspace": "/workspace",
                            "compute": {"kind": "docker_sandbox", "template": TEMPLATE},
                        },
                        "sandbox": sandbox,
                    }
                )
            )
            result = subprocess.run(
                [binary, "sandbox", "--", "/usr/bin/true"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode != 0 and not result.stdout, result
            assert "standalone" in result.stderr and "compute" in result.stderr, (
                result.stderr
            )
            records.append({"sandbox": sandbox, "error": result.stderr})
        return records


def test_incompatible_provider_and_compute_configuration(binary: Path) -> Transcript:
    cases = [
        (
            {"sandbox": {"provider": "compute"}},
            "requires target.compute.kind: docker_sandbox",
        ),
        ({"sandbox": {"provider": "none"}}, "sandbox.provider"),
        (
            {
                "target": {
                    "transport": {"kind": "ssh", "host": "unused"},
                    "workspace": "/workspace",
                    "compute": {"kind": "docker_sandbox", "template": TEMPLATE},
                }
            },
            "docker_sandbox requires local transport",
        ),
        (
            {
                "target": {
                    "workspace": "/workspace",
                    "compute": {"kind": "docker", "image": "example"},
                },
                "sandbox": {"provider": "compute"},
            },
            "requires target.compute.kind: docker_sandbox",
        ),
        (
            {
                "target": {
                    "workspace": "/workspace",
                    "compute": {"kind": "docker_sandbox", "template": TEMPLATE},
                },
                "sandbox": {"provider": "native"},
            },
            "only supports sandbox.provider: compute",
        ),
        (
            {
                "target": {
                    "workspace": "/workspace",
                    "compute": {
                        "kind": "docker_sandbox",
                        "template": "example:mutable",
                    },
                }
            },
            "digest-qualified",
        ),
        (
            {
                "target": {
                    "workspace": "/workspace",
                    "compute": {
                        "kind": "docker_sandbox",
                        "template": TEMPLATE,
                        "mounts": [{"source": ".", "target": "/remapped"}],
                    },
                }
            },
            "same absolute path",
        ),
    ]
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        for value, expected in cases:
            config.write_text(json.dumps(value))
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
            records.append({"config": value, "error": result.stderr})
    return records


if __name__ == "__main__":
    run_this_suite(__file__)
