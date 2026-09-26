#!/usr/bin/env -S uv run --script

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.client import McpClient
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, SANDBOX, requires
from support.suites import run_this_suite


CONFIG = ".agents/console/config.yaml"


def configure(workspace: Path, value: object) -> None:
    config = workspace / CONFIG
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(value))


@requires(SANDBOX, NATIVE_FIXTURES)
def test_layers_project_then_cli_in_order(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        (workspace / "project").mkdir()
        (workspace / "cli").mkdir()
        configure(
            workspace,
            {
                "extends": ":read-only",
                "sandbox": {
                    "environment": {"KEEP": "project", "CHANGE": "project"},
                    "workspace_options": {"exclude_slash_tmp": True},
                    "filesystem": {
                        "kind": "restricted",
                        "entries": [
                            {
                                "path": {"type": "path", "path": "./project"},
                                "access": "write",
                            }
                        ],
                    },
                },
            },
        )
        capture = workspace / "payloads.jsonl"
        environment = {
            **os.environ,
            LOADER_VARIABLE: str(build_interposer(workspace, "runner_configuration")),
            "MCP_CONSOLE_TEST_RUNNER_CONFIGURATION": str(capture),
        }
        overrides = [
            "-c",
            "sandbox.environment.BEFORE=first",
            "-c",
            "sandbox.environment.CHANGE=first",
            "-c",
            "sandbox={environment: {CHANGE=last, ADD: cli}, filesystem: "
            "{entries: [{path={type=path, path='./cli'}, access=write}], "
            "glob_scan_max_depth: 3}}",
            "-c",
            "extends=null",
            "-c",
            "sandbox.workspace_options=null",
        ]
        for command in ("serve", "sandbox"):
            arguments = [*overrides[:2], command, *overrides[2:]]
            if command == "serve":
                with McpClient(
                    binary,
                    (*arguments, "--worker", "unused-worker"),
                    current_directory=workspace,
                    environment=environment,
                ) as client:
                    client.initialize_and_list_tools()
                    _, stderr = client.finish_with_standard_error()
                    assert stderr == "", stderr
            else:
                result = subprocess.run(
                    [binary, *arguments, "--", "/usr/bin/true"],
                    cwd=workspace,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert result.returncode == 0 and result.stderr == "", result
            payloads = [json.loads(line) for line in capture.read_text().splitlines()]
            for payload in payloads:
                assert "extends" not in payload, payload
                assert payload["environment"] == {
                    "KEEP": "project",
                    "CHANGE": "last",
                    "ADD": "cli",
                    "BEFORE": "first",
                }, payload
                assert payload["workspace_options"] is None, payload
                filesystem = payload["filesystem"]
                assert filesystem["kind"] == "restricted", filesystem
                assert filesystem["glob_scan_max_depth"] == 3, filesystem
                paths = [
                    entry["path"]["path"]
                    for entry in filesystem["entries"]
                    if entry["path"]["type"] == "path"
                ]
                assert paths == [str(workspace / "cli")], filesystem
            records.append(
                {
                    "command": command,
                    "overrides": overrides,
                    "environment": payloads[-1]["environment"],
                    "workspace_options": payloads[-1]["workspace_options"],
                    "glob_scan_max_depth": filesystem["glob_scan_max_depth"],
                    "paths": [str(Path(path).relative_to(workspace)) for path in paths],
                }
            )
            capture.unlink()
    return records


@requires(SANDBOX)
def test_inline_strings_and_objects_without_project_file(binary: Path) -> Transcript:
    expected = {
        "MESSAGE": "a, [b] = {c}: #雪",
        "NUMBER": "42",
        "EMPTY": "",
        "URL": "https://example.com/a=b",
        "APOSTROPHE": "it's",
        "ESCAPE": 'say "go" \\ end',
        "APP.VERSION": "v1",
    }
    cases = (
        "{MESSAGE: 'a, [b] = {c}: #雪', NUMBER: '42', EMPTY: '', URL: https://example.com/a=b, "
        r"""APOSTROPHE: 'it''s', ESCAPE: "say \"go\" \\ end", APP.VERSION: v1}""",
        json.dumps(expected, separators=(",", ":")),
        "{MESSAGE = 'a, [b] = {c}: #雪', NUMBER = '42', EMPTY = '', URL = 'https://example.com/a=b', "
        r"""APOSTROPHE = "it's", ESCAPE = "say \"go\" \\ end", "APP.VERSION" = 'v1',}""",
    )
    records = []
    with TemporaryDirectory() as temporary:
        for value in cases:
            result = subprocess.run(
                [
                    binary,
                    "sandbox",
                    "--config",
                    f"sandbox.environment={value}",
                    "--",
                    sys.executable,
                    "-c",
                    # fmt: python
                    code("""
                        import json
                        import os
                        import sys

                        print(json.dumps({key: os.environ[key] for key in sys.argv[1:]}))
                        """),
                    *expected,
                ],
                cwd=temporary,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 0 and result.stderr == "", result
            actual = json.loads(result.stdout)
            assert actual == expected, actual
            records.append({"value": value, "environment": actual})
    return records


def test_overrides_precede_schema_validation(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        configure(workspace, {"sandbox": False, "target": ["invalid"]})
        overrides = (
            "-c",
            "sandbox.environment.KEEP=first",
            "-c",
            "sandbox.environment=null",
            "-c",
            "sandbox.environment={FINAL: last}",
            "-c",
            "target=null",
        )
        with McpClient(
            binary,
            ("serve", "--no-sandbox", *overrides),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            _, stderr = client.finish_with_standard_error()
            assert stderr == "", stderr
        assert json.loads((workspace / CONFIG).read_text()) == {
            "sandbox": False,
            "target": ["invalid"],
        }
    return [{"overrides": list(overrides), "initialized": True}]


def test_discovers_home_configuration_with_project_precedence(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        home = root / "home"
        workspace = root / "project"
        home.mkdir()
        workspace.mkdir()
        environment = {**os.environ, "HOME": str(home)}
        environment.pop("MCP_CONSOLE_HOME", None)
        configure(home, {"unknown": "home"})

        def launch_error() -> str:
            result = subprocess.run(
                [binary, "serve", "--no-sandbox"],
                cwd=workspace,
                env=environment,
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 1 and result.stdout == "", result
            return result.stderr

        assert str(home / CONFIG) in launch_error()

        fixture_workspace = root / "default-client"
        fixture_workspace.mkdir()
        with McpClient(
            binary,
            ("serve", "--no-sandbox"),
            environment=environment,
            current_directory=fixture_workspace,
        ) as client:
            client.initialize_and_list_tools()
            _, stderr = client.finish_with_standard_error()
            assert stderr == "", stderr

        opt_out_workspace = root / "opt-out-client"
        opt_out_workspace.mkdir()
        with McpClient(
            binary,
            ("serve", "--no-sandbox"),
            environment=environment,
            current_directory=opt_out_workspace,
            record_in_project=False,
        ) as client:
            client.initialize_and_list_tools()
            _, stderr = client.finish_with_standard_error()
            assert stderr == "", stderr
        assert not (opt_out_workspace / ".agents").exists()

        configure(workspace, {})
        with McpClient(
            binary,
            ("serve", "--no-sandbox"),
            environment=environment,
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            _, stderr = client.finish_with_standard_error()
            assert stderr == "", stderr

        configure(workspace, {"unknown": "project"})
        error = launch_error()
        assert CONFIG in error and str(home / CONFIG) not in error, error

        (workspace / CONFIG).unlink()
        assert str(home / CONFIG) in launch_error()
        configure(home, {})
        with McpClient(
            binary,
            ("serve", "--no-sandbox"),
            environment=environment,
            current_directory=workspace,
            record_in_project=False,
            use_home_configuration=True,
        ) as client:
            client.initialize_and_list_tools()
            _, stderr = client.finish_with_standard_error()
            assert stderr == "", stderr

    return [{"home_fallback": True, "project_precedence": True}]


def test_discovers_explicit_console_home(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        home = root / "home"
        console_home = root / "console"
        workspace = root / "workspace"
        console_home.mkdir()
        workspace.mkdir()
        configure(home, {"unknown": "ambient home"})
        selected_config = console_home / "config.yaml"
        selected_config.write_text('{"unknown": "selected console home"}')
        environment = os.environ | {
            "HOME": str(home),
            "MCP_CONSOLE_HOME": str(console_home),
        }
        for arguments in (("serve", "--no-sandbox"), ("sandbox", "--", "unused")):
            result = subprocess.run(
                [binary, *arguments],
                cwd=workspace,
                env=environment,
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 1 and result.stdout == "", result
            assert str(selected_config) in result.stderr, result.stderr
            assert str(home / CONFIG) not in result.stderr, result.stderr

        configure(workspace, {})
        with McpClient(
            binary,
            ("serve", "--no-sandbox"),
            environment,
            workspace,
            record_in_project=False,
            use_home_configuration=True,
        ) as client:
            client.initialize_and_list_tools()
            _, stderr = client.finish_with_standard_error()
            assert stderr == "", stderr

        (workspace / CONFIG).unlink()
        selected_config.unlink()
        with McpClient(
            binary,
            ("serve", "--no-sandbox"),
            environment,
            workspace,
            record_in_project=False,
            use_home_configuration=True,
        ) as client:
            client.initialize_and_list_tools()
            _, stderr = client.finish_with_standard_error()
            assert stderr == "", stderr

        for invalid in ("", "relative/path"):
            result = subprocess.run(
                [binary, "serve", "--no-sandbox"],
                cwd=workspace,
                env=environment | {"MCP_CONSOLE_HOME": invalid},
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 1 and result.stdout == "", result
            assert "MCP_CONSOLE_HOME must be an absolute path" in result.stderr, (
                result.stderr
            )
    return [
        {
            "console_home_override": True,
            "project_precedence": True,
            "invalid_console_home_rejected": True,
        }
    ]


def test_rejects_malformed_overrides(binary: Path) -> Transcript:
    cases = (
        "sandbox",
        "=value",
        "sandbox..network=restricted",
        "sandbox.=restricted",
        "sandbox.network=",
        "sandbox={network:}",
        "sandbox={network restricted}",
        "sandbox={network: [restricted}",
        "sandbox={network: 'restricted}",
        "sandbox={network: restricted} trailing",
        "sandbox={network: [restricted,, enabled]}",
        "extends=.inf",
        "extends=first\nsecond",
    )
    records = []
    with TemporaryDirectory() as temporary:
        for override in cases:
            result = subprocess.run(
                [binary, "serve", "--no-sandbox", "-c", override],
                cwd=temporary,
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 1 and result.stdout == "", result
            assert "configuration override" in result.stderr, result.stderr
            records.append({"override": override, "stderr": result.stderr})
    return records


def test_validates_effective_configuration(binary: Path) -> Transcript:
    cases = (
        ("extends=true", "boolean"),
        ("extends=42", "integer"),
        ("extends=1.5", "floating point"),
        ("target.command=[far, faz]", "local host target"),
        ("unknown={baz: [far, faz]}", "unknown field"),
    )
    records = []
    with TemporaryDirectory() as temporary:
        for override, expected in cases:
            result = subprocess.run(
                [binary, "serve", "--no-sandbox", "-c", override],
                cwd=temporary,
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 1 and result.stdout == "", result
            assert expected in result.stderr, result.stderr
            records.append({"override": override, "stderr": result.stderr})
    return records


def test_overrides_do_not_bypass_file_errors_or_explicit_inputs(
    binary: Path,
) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        config = workspace / CONFIG
        config.parent.mkdir(parents=True)
        config.write_text("sandbox: [")
        cases = (
            (["serve", "--no-sandbox", "-c", "sandbox={} "], CONFIG),
            (
                [
                    "-c",
                    "extends=:workspace",
                    "sandbox",
                    "--config-env",
                    "TEST_POLICY",
                    "--",
                    "/usr/bin/true",
                ],
                "cannot be combined",
            ),
            (
                [
                    "sandbox",
                    "--settings-env",
                    "TEST_POLICY",
                    "-c",
                    "extends=:workspace",
                    "--",
                    "/usr/bin/true",
                ],
                "cannot be combined",
            ),
        )
        for arguments, expected in cases:
            result = subprocess.run(
                [binary, *arguments],
                cwd=workspace,
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 1 and result.stdout == "", result
            assert expected in result.stderr, result.stderr
            records.append({"arguments": arguments, "stderr": result.stderr})
    return records


if __name__ == "__main__":
    run_this_suite(__file__)
