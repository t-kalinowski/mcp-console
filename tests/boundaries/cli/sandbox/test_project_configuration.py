#!/usr/bin/env -S uv run --script

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.normalization import code
from support.records import Transcript
from support.requirements import SANDBOX, requires
from support.suites import run_this_suite


LOCATIONS = (".mcp-console/config.yaml", ".agents/mcp-console.yaml")


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


def test_rejects_invalid_project_configuration(binary: Path) -> Transcript:
    cases = (
        (
            "custom core scalar tag",
            "sandbox: {network: !!python/object:example 'enabled'}",
            "tag",
        ),
        ("invalid tagged scalar", "sandbox: {network: !!int 'enabled'}", "scalar"),
        ("network mapping", "sandbox: {network: {enabled: null}}", "network"),
        (
            "proxy mode mapping",
            "sandbox: {proxy: {enabled: true, mode: {full: null}}}",
            "mode",
        ),
        (
            "domain permission mapping",
            "sandbox: {proxy: {enabled: true, domains: {example.com: {allow: null}}}}",
            "domains",
        ),
        (
            "filesystem kind mapping",
            "sandbox: {filesystem: {kind: {restricted: null}}}",
            "kind",
        ),
        (
            "path type mapping",
            "sandbox: {filesystem: {entries: [{path: {type: {path: null}, path: ./out}, access: write}]}}",
            "type",
        ),
        (
            "access mapping",
            "sandbox: {filesystem: {entries: [{path: {type: path, path: ./out}, access: {write: null}}]}}",
            "access",
        ),
        ("empty", "", "one mapping document"),
        ("sequence", "[]", "mapping"),
        ("multiple documents", "---\n{}\n---\n{}", "one mapping document"),
        ("malformed", "sandbox: [", "line"),
        ("top-level field", "profile: default", "profile"),
        ("sandbox type", "sandbox: false", "sandbox"),
        ("sandbox field", "sandbox: {cwd: /tmp}", "cwd"),
        ("filesystem type", "sandbox: {filesystem: []}", "filesystem"),
        ("filesystem kind", "sandbox: {filesystem: {kind: unrestricted}}", "kind"),
        ("filesystem field", "sandbox: {filesystem: {roots: []}}", "roots"),
        ("entries type", "sandbox: {filesystem: {entries: {}}}", "entries"),
        (
            "entry form",
            "sandbox: {filesystem: {entries: [{path: ./out, access: write}]}}",
            "path",
        ),
        (
            "special path",
            "sandbox: {filesystem: {entries: [{path: {type: special, value: {kind: root}}, access: write}]}}",
            "type",
        ),
        (
            "read grant",
            "sandbox: {filesystem: {entries: [{path: {type: path, path: ./out}, access: read}]}}",
            "access",
        ),
        (
            "path type",
            "sandbox: {filesystem: {entries: [{path: {type: path, path: 42}, access: write}]}}",
            "path",
        ),
        (
            "path field",
            "sandbox: {filesystem: {entries: [{path: {type: path, path: ./out, extra: true}, access: write}]}}",
            "extra",
        ),
        (
            "entry field",
            "sandbox: {filesystem: {entries: [{path: {type: path, path: ./out}, access: write, extra: true}]}}",
            "extra",
        ),
        ("network value", "sandbox: {network: full}", "network"),
        ("network type", "sandbox: {network: true}", "network"),
        ("proxy type", "sandbox: {proxy: true}", "proxy"),
        ("proxy disabled", "sandbox: {proxy: {enabled: false}}", "enabled"),
        ("proxy missing enabled", "sandbox: {proxy: {}}", "enabled"),
        ("YAML 1.2 boolean", "sandbox: {proxy: {enabled: yes}}", "enabled"),
        ("proxy mode", "sandbox: {proxy: {enabled: true, mode: enabled}}", "mode"),
        ("proxy domains", "sandbox: {proxy: {enabled: true, domains: []}}", "domains"),
        (
            "domain permission",
            "sandbox: {proxy: {enabled: true, domains: {example.com: ask}}}",
            "domains",
        ),
        (
            "proxy option type",
            "sandbox: {proxy: {enabled: true, enableSocks5: null}}",
            "enableSocks5",
        ),
        (
            "private proxy option",
            "sandbox: {proxy: {enabled: true, enableSocks5Udp: true}}",
            "enableSocks5Udp",
        ),
        ("duplicate top-level", "sandbox: {}\nsandbox: {}", "duplicate"),
        (
            "duplicate nested",
            "sandbox: {network: restricted, network: enabled}",
            "duplicate",
        ),
        (
            "duplicate quoted key",
            "sandbox: {proxy: {enabled: true, domains: {example.com: allow, 'example.com': deny}}}",
            "duplicate",
        ),
        ("custom scalar tag", "sandbox: {network: !custom restricted}", "tag"),
        ("custom collection tag", "sandbox: !custom {}", "tag"),
        (
            "non-string key",
            "sandbox: {proxy: {enabled: true, domains: {1: allow}}}",
            "key",
        ),
        ("merge key", "sandbox: {<<: {network: enabled}}", "<<"),
    )
    transcript = []
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        config = host / LOCATIONS[0]
        config.parent.mkdir()
        for name, yaml, diagnostic in cases:
            config.write_text(yaml, encoding="utf-8")
            for arguments in ((), ("sandbox", "--", "/bin/echo", "workload started")):
                result = invoke(binary, host, *arguments)
                assert result.returncode == 1, (name, result)
                assert result.stdout == "", (name, result)
                assert LOCATIONS[0] in result.stderr, (name, result)
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
        for location in LOCATIONS:
            config = host / location
            config.parent.mkdir()
        accepted(binary, host)
        transcript.append({"case": "absent", "initialized": True})
        for location in LOCATIONS:
            metadata = (host / location).parent
            metadata.rmdir()
            metadata.write_text("not a metadata directory", encoding="utf-8")
            accepted(binary, host)
            transcript.append(
                {"case": f"{metadata.name} is a file", "initialized": True}
            )
            metadata.unlink()
            metadata.mkdir()
        for location in LOCATIONS:
            config = host / location
            config.write_text("sandbox: {network: invalid}", encoding="utf-8")
            result = invoke(binary, host)
            assert result.returncode == 1 and location in result.stderr, result
            transcript.append(
                {
                    "case": location,
                    "stderr": result.stderr.replace(str(host), "<project>"),
                }
            )
            config.unlink()
        for location in LOCATIONS:
            (host / location).write_text("invalid", encoding="utf-8")
        result = invoke(binary, host)
        assert result.returncode == 1 and "ambiguous" in result.stderr, result
        assert all(location in result.stderr for location in LOCATIONS), result
        transcript.append(
            {
                "case": "ambiguous",
                "stderr": result.stderr.replace(str(host), "<project>"),
            }
        )
        child = host / "child"
        child.mkdir()
        accepted(binary, child)
        transcript.append({"case": "ancestors ignored", "initialized": True})
        for location in LOCATIONS:
            (host / location).unlink()
        config = host / LOCATIONS[0]
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


def test_accepts_supported_project_settings(binary: Path) -> Transcript:
    cases = (
        "sandbox: {proxy: {enabled: !!bool 'true'}}",
        "{}",
        "sandbox: {}",
        "sandbox: {proxy: null}",
        "sandbox: {filesystem: {entries: [{path: {type: path, path: './future café 雪'}, access: write}]}}",
        "sandbox: {network: enabled, proxy: {enabled: true, mode: limited, enableSocks5: false, allowUpstreamProxy: true, allowLocalBinding: true, domains: {'*.example.com': allow, blocked.example.com: deny, ignored.example.com: none}}}",
    )
    transcript = []
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        config = host / LOCATIONS[1]
        config.parent.mkdir()
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
        for location in LOCATIONS:
            config = host / location
            config.parent.mkdir()
            config.write_text("invalid: [", encoding="utf-8")
        accepted(binary, host, "serve", "--no-sandbox", "--worker", "unused-worker")
    return [
        {
            "arguments": ["serve", "--no-sandbox"],
            "ambiguous_invalid_files_ignored": True,
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
        for location in LOCATIONS:
            config = host / location
            config.parent.mkdir()
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
