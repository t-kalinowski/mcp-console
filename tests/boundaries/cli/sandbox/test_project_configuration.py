#!/usr/bin/env -S uv run --script

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.normalization import code
from support.records import Transcript
from support.requirements import SANDBOX, requires
from support.sandbox_configuration import NATIVE_PROXY
from support.suites import run_this_suite


CONFIG = ".agents/console/config.yaml"


def accepted(binary: Path, host: Path, *arguments: str) -> None:
    with McpClient(
        binary,
        arguments or ("serve", "--worker", "unused-worker"),
        current_directory=host,
    ) as client:
        client.initialize_and_list_tools()
        _, stderr = client.finish_with_standard_error()
        assert stderr == "", stderr


def invoke(binary: Path, host: Path, *arguments: str):
    return subprocess.run(
        [binary, *(arguments or ("serve", "--worker", "unused-worker"))],
        cwd=host,
        env={**os.environ, "MCP_CONSOLE_SANDBOX_SETTINGS": "invalid ambient settings"},
        input="",
        capture_output=True,
        text=True,
    )


@requires(SANDBOX)
def test_duplicate_keys_use_last_value(binary: Path) -> Transcript:
    proxy_fields = json.dumps(NATIVE_PROXY)[1:-1]
    cases = (
        "sandbox: invalid\nsandbox: {}",
        "sandbox: {network: invalid, network: restricted}",
        "sandbox: {proxy: {"
        + proxy_fields
        + ", domains: {example.com: invalid, 'example.com': deny}}}",
    )
    with TemporaryDirectory() as directory:
        host = Path(directory)
        config = host / CONFIG
        config.parent.mkdir(parents=True)
        for yaml in cases:
            config.write_text(yaml, encoding="utf-8")
            accepted(binary, host)
    return [{"yaml": yaml, "initialized": True} for yaml in cases]


@requires(SANDBOX)
def test_native_validation_precedes_server_readiness(binary: Path) -> Transcript:
    proxy_cases = (
        ("proxy disabled", {**NATIVE_PROXY, "enabled": False}),
        (
            "proxy missing enabled",
            {key: value for key, value in NATIVE_PROXY.items() if key != "enabled"},
        ),
        ("proxy mode", {**NATIVE_PROXY, "mode": "enabled"}),
        ("proxy domains", {**NATIVE_PROXY, "domains": []}),
        ("domain permission", {**NATIVE_PROXY, "domains": {"example.com": "ask"}}),
        ("proxy option type", {**NATIVE_PROXY, "enableSocks5": None}),
        ("native domain pattern", {**NATIVE_PROXY, "domains": {"[": "allow"}}),
        ("proxy type", True),
    )
    cases = (
        ("network value", "sandbox: {network: full}"),
        ("network type", "sandbox: {network: true}"),
        (
            "YAML 1.2 boolean",
            "sandbox: {proxy: {" + json.dumps(NATIVE_PROXY)[1:-1] + ", enabled: yes}}",
        ),
        (
            "path type mapping",
            "sandbox: {filesystem: {entries: [{path: {type: {path: null}, path: ./out}, access: write}]}}",
        ),
        ("filesystem type", "sandbox: {filesystem: []}"),
        ("entries type", "sandbox: {filesystem: {entries: {}}}"),
        (
            "entry form",
            "sandbox: {filesystem: {entries: [{path: ./out, access: write}]}}",
        ),
        (
            "path type",
            "sandbox: {filesystem: {entries: [{path: {type: path, path: 42}, access: write}]}}",
        ),
        ("sandbox field", "sandbox: {cwd: /tmp}"),
        ("merge key", "sandbox: {<<: {network: enabled}}"),
        *(
            (name, json.dumps({"sandbox": {"proxy": proxy}}))
            for name, proxy in proxy_cases
        ),
    )
    transcript = []
    with TemporaryDirectory() as directory:
        host = Path(directory)
        config = host / CONFIG
        config.parent.mkdir(parents=True)
        for name, yaml in cases:
            config.write_text(yaml, encoding="utf-8")
            for arguments in ((), ("sandbox", "--", "/bin/echo", "workload started")):
                result = invoke(binary, host, *arguments)
                assert result.returncode == 1 and result.stdout == "", (name, result)
                assert f"{CONFIG}: sandbox preflight failed" in result.stderr, (
                    name,
                    result,
                )
                # Native JSON offsets include platform policy and the launch PID.
                stderr = re.sub(
                    r"(at line [0-9]+, column )[0-9]+", r"\1<column>", result.stderr
                )
                transcript.append(
                    {
                        "case": name,
                        "command": arguments[0] if arguments else "serve",
                        "stderr": stderr,
                    }
                )
    return transcript


def test_rejects_invalid_project_configuration(binary: Path) -> Transcript:
    cases = (
        ("invalid tagged scalar", "sandbox: {network: !!int enabled}", "YAML"),
        ("empty", "", "one mapping document"),
        ("sequence", "[]", "mapping"),
        ("multiple documents", "---\n{}\n---\n{}", "one mapping document"),
        ("malformed", "sandbox: [", "line"),
        ("top-level field", "profile: default", "profile"),
        ("sandbox type", "sandbox: false", "sandbox"),
        ("sandbox sequence", "sandbox: []", "sandbox"),
        ("owned protocol", "sandbox: {version: 2}", "version is managed by Console"),
        (
            "owned lifetime",
            "sandbox: {lifecycle: {parent_pid: null}}",
            "lifecycle is managed by Console",
        ),
        ("custom scalar tag", "sandbox: {network: !custom restricted}", "tag"),
        ("custom collection tag", "sandbox: !custom {}", "tag"),
        (
            "non-string key",
            "sandbox: {proxy: {enabled: true, domains: {1: allow}}}",
            "key",
        ),
        ("non-finite number", "sandbox: {future: .inf}", "non-finite"),
    )
    transcript = []
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        config = host / CONFIG
        config.parent.mkdir(parents=True)
        for name, yaml, diagnostic in cases:
            config.write_text(yaml, encoding="utf-8")
            for arguments in ((), ("sandbox", "--", "/bin/echo", "workload started")):
                result = invoke(binary, host, *arguments)
                assert result.returncode == 1, (name, result)
                assert result.stdout == "", (name, result)
                assert CONFIG in result.stderr, (name, result)
                assert diagnostic in result.stderr, (name, result)
                transcript.append(
                    {
                        "case": name,
                        "command": arguments[0] if arguments else "serve",
                        "stderr": result.stderr.replace(str(host), "<project>"),
                    }
                )
    return transcript


def test_discovers_only_launch_directory_configuration(binary: Path) -> Transcript:
    transcript = []
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        accepted(binary, host)
        assert not (host / ".agents").exists()
        transcript.append({"case": "absent", "initialized": True})
        for location in (".agents", ".agents/console"):
            metadata = host / location
            metadata.parent.mkdir(parents=True, exist_ok=True)
            metadata.write_text("not a metadata directory", encoding="utf-8")
            accepted(binary, host)
            transcript.append({"case": f"{location} is a file", "initialized": True})
            metadata.unlink()

        for location in (".mcp-console/config.yaml", ".agents/mcp-console.yaml"):
            old = host / location
            old.parent.mkdir(parents=True, exist_ok=True)
            old.write_text("invalid: [", encoding="utf-8")
            accepted(binary, host)
            transcript.append({"case": f"ignored {location}", "initialized": True})

        config = host / CONFIG
        config.parent.mkdir()
        config.write_text("{}", encoding="utf-8")
        accepted(binary, host)
        transcript.append({"case": "only console config used", "initialized": True})

        config.write_text("unknown: true", encoding="utf-8")
        for arguments in ((), ("sandbox", "--", "/bin/echo", "workload started")):
            result = invoke(binary, host, *arguments)
            assert result.returncode == 1 and result.stdout == "", result
            assert CONFIG in result.stderr and "unknown" in result.stderr, result
            transcript.append(
                {
                    "case": CONFIG,
                    "command": arguments[0] if arguments else "serve",
                    "stderr": result.stderr.replace(str(host), "<project>"),
                }
            )

        child = host / "child"
        child.mkdir()
        accepted(binary, child)
        transcript.append({"case": "ancestors ignored", "initialized": True})

        config.unlink()
        config.mkdir()
        result = invoke(binary, host)
        assert result.returncode == 1 and "cannot read" in result.stderr, result
        transcript.append(
            {
                "case": "unreadable existing path",
                "stderr": result.stderr.replace(str(host), "<project>"),
            }
        )
    return transcript


@requires(SANDBOX)
def test_accepts_supported_project_settings(binary: Path) -> Transcript:
    cases = (
        "sandbox: {proxy: {"
        + json.dumps(NATIVE_PROXY)[1:-1]
        + ", enabled: !!bool true}}",
        "{}",
        "sandbox: {}",
        "sandbox: {proxy: null}",
        "sandbox: {filesystem: {kind: {restricted: null}}}",
        "sandbox: {filesystem: {entries: [{path: {type: path, path: ./out}, access: {write: null}}]}}",
        "sandbox: {filesystem: {entries: [{path: {type: path, path: './future café 雪'}, access: write}]}}",
        json.dumps(
            {
                "sandbox": {
                    "network": "enabled",
                    "proxy": {
                        **NATIVE_PROXY,
                        "mode": "limited",
                        "enableSocks5": False,
                        "allowUpstreamProxy": True,
                        "allowLocalBinding": True,
                        "domains": {
                            "*.example.com": "allow",
                            "blocked.example.com": "deny",
                            "ignored.example.com": "none",
                        },
                    },
                }
            }
        ),
    )
    transcript = []
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        config = host / CONFIG
        config.parent.mkdir(parents=True)
        for yaml in cases:
            config.write_text(yaml, encoding="utf-8")
            accepted(
                binary,
                host,
                "serve",
                "--worker",
                "unused-worker",
                "--writable-root",
                "future CLI",
            )
            assert not (host / "future café 雪").exists()
            assert not (host / "future CLI").exists()
            transcript.append({"yaml": yaml, "initialized": True})
    return transcript


def test_no_sandbox_bypasses_project_configuration(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        host = Path(directory)
        config = host / CONFIG
        config.parent.mkdir(parents=True)
        config.write_text("invalid: [", encoding="utf-8")
        accepted(binary, host, "serve", "--no-sandbox", "--worker", "unused-worker")
    return [
        {
            "arguments": ["serve", "--no-sandbox"],
            "invalid_config_ignored": True,
        }
    ]


@requires(SANDBOX)
def test_explicit_policy_bypasses_project_configuration(binary: Path) -> Transcript:
    script = code("""
        import os
        import sys

        assert "MCP_CONSOLE_SANDBOX_SETTINGS" not in os.environ
        assert "TEST_POLICY" not in os.environ
        print(sys.stdin.read())
        """)
    policy = {
        "version": 2,
        "filesystem": {
            "kind": "restricted",
            "entries": [
                {
                    "path": {"type": "special", "value": {"kind": "root"}},
                    "access": "read",
                },
            ],
        },
        "network": "restricted",
    }
    transcript = []
    with TemporaryDirectory() as directory:
        host = Path(directory)
        config = host / CONFIG
        config.parent.mkdir(parents=True)
        config.write_text("invalid: [", encoding="utf-8")
        # Also exercise a selected complete policy whose name is reserved for
        # internal application settings. Only the explicit selector owns it.
        for name in ("TEST_POLICY", "MCP_CONSOLE_SANDBOX_SETTINGS"):
            result = subprocess.run(
                [
                    binary,
                    "sandbox",
                    "--config-env",
                    name,
                    "--",
                    sys.executable,
                    "-c",
                    script,
                ],
                cwd=host,
                env={
                    **os.environ,
                    "MCP_CONSOLE_SANDBOX_SETTINGS": "invalid ambient settings",
                    name: json.dumps(policy),
                },
                input="unchanged workload stdin",
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0 and result.stderr == "", result
            assert result.stdout == "unchanged workload stdin\n", result
            transcript.append({"selected": name, "stdout": result.stdout})
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
