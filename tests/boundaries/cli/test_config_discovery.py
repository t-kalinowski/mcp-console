#!/usr/bin/env -S uv run --script

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.client import McpClient
from support.normalization import code
from support.records import Transcript
from support.requirements import R, SANDBOX, requires
from support.snapshots import platform_snapshots
from support.suites import run_this_suite


CONFIG = ".agents/console/config.yaml"
LABEL = "MCP_CONSOLE_TEST_DISCOVERY"


def write_config(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def observe_environment(
    binary: Path,
    workspace: Path,
    environment: dict[str, str],
    arguments: tuple[str, ...],
    expected: str,
) -> None:
    if "serve" in arguments:
        with McpClient(
            binary,
            arguments,
            environment=environment,
            current_directory=workspace,
            use_home_configuration=True,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                expected + "\n",
                # fmt: r
                r=code("""
                    cat(
                      Sys.getenv(c("MCP_CONSOLE_TEST_DISCOVERY", "KEEP", "ADDED"), unset = "unset"),
                      sep = "|"
                    )
                    cat("\n")
                    """),
            )
            _, stderr = client.finish_with_standard_error()
            assert stderr == "", stderr
    else:
        # fmt: python
        script = code("""
            import os

            print(
                "|".join(
                    os.environ.get(name, "unset")
                    for name in ("MCP_CONSOLE_TEST_DISCOVERY", "KEEP", "ADDED")
                )
            )
            """)
        result = subprocess.run(
            [binary, *arguments, "--", sys.executable, "-c", script],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0 and result.stderr == "", result
        assert result.stdout == expected + "\n", result


@requires(SANDBOX, R)
def test_selects_project_or_home_in_both_commands(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        home = root / "console-home"
        write_config(
            home / "config.yaml",
            {"cache": "host", "sandbox": {"environment": {LABEL: "home"}}},
        )
        environment = os.environ | {"MCP_CONSOLE_HOME": str(home)}
        for name in ("KEEP", "ADDED"):
            environment.pop(name, None)
        for command in ("serve", "sandbox"):
            project = workspace / CONFIG
            write_config(
                project,
                {"cache": "host", "sandbox": {"environment": {LABEL: "project"}}},
            )
            cases = (
                ("default", (command,), "project"),
                ("before", ("--no-project-config", command), "home"),
                ("after", (command, "--no-project-config"), "home"),
            )
            for placement, arguments, label in cases:
                observe_environment(
                    binary, workspace, environment, arguments, label + "|unset|unset"
                )
                records.append(
                    {"command": command, "flag": placement, "selected": label}
                )
            project.unlink()
            for arguments in ((command,), (command, "--no-project-config")):
                observe_environment(
                    binary, workspace, environment, arguments, "home|unset|unset"
                )
                records.append(
                    {
                        "command": command,
                        "arguments": list(arguments),
                        "project": "absent",
                        "selected": "home",
                    }
                )
    return records


@requires(SANDBOX, R)
def test_layers_ordered_overrides_on_home_and_defaults(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        home = root / "console-home"
        home.mkdir()
        write_config(workspace / CONFIG, {"unknown": "must be skipped"})
        environment = os.environ | {"MCP_CONSOLE_HOME": str(home)}
        for name in (LABEL, "KEEP", "ADDED"):
            environment.pop(name, None)
        for configured in (False, True):
            if configured:
                write_config(
                    home / "config.yaml",
                    {"sandbox": {"environment": {LABEL: "home", "KEEP": "home"}}},
                )
            for command in ("serve", "sandbox"):
                arguments = (
                    "--no-project-config",
                    "-c",
                    "cache=invalid",
                    "-c",
                    "sandbox.environment.MCP_CONSOLE_TEST_DISCOVERY=42",
                    command,
                    "-c",
                    "cache=host",
                    "-c",
                    "sandbox.environment={MCP_CONSOLE_TEST_DISCOVERY: last, ADDED: cli}",
                )
                expected = "last|" + ("home" if configured else "unset") + "|cli"
                observe_environment(binary, workspace, environment, arguments, expected)
                records.append(
                    {"command": command, "home": configured, "environment": expected}
                )
        (home / "config.yaml").unlink()
        for command in ("serve", "sandbox"):
            observe_environment(
                binary,
                workspace,
                environment,
                (command, "--no-project-config"),
                "unset|unset|unset",
            )
            records.append(
                {
                    "command": command,
                    "home": False,
                    "overrides": False,
                    "environment": "unset|unset|unset",
                }
            )
    return records


@platform_snapshots("win32")
def test_skips_invalid_project_configuration(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        workspace = root / "workspace"
        home = root / "console-home"
        workspace.mkdir()
        home.mkdir()
        (home / "config.yaml").write_text("{}", encoding="utf-8")
        project = workspace / CONFIG
        project.parent.mkdir(parents=True)
        environment = os.environ | {"MCP_CONSOLE_HOME": str(home)}
        for state in ("malformed", "unreadable"):
            if state == "malformed":
                project.write_text("sandbox: [", encoding="utf-8")
            else:
                project.unlink()
                project.mkdir()
            result = subprocess.run(
                [binary, "serve", "--no-sandbox", "--worker", "unused-worker"],
                cwd=workspace,
                env=environment,
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 1 and result.stdout == "", result
            assert CONFIG in result.stderr, result.stderr
            with McpClient(
                binary,
                (
                    "serve",
                    "--no-project-config",
                    "--no-sandbox",
                    "--worker",
                    "unused-worker",
                ),
                environment=environment,
                current_directory=workspace,
                use_home_configuration=True,
            ) as client:
                client.initialize_and_list_tools()
                _, stderr = client.finish_with_standard_error()
                assert stderr == "", stderr
            records.append(
                {"project": state, "default_error": result.stderr, "skipped": True}
            )
    return records


@platform_snapshots("win32")
def test_retains_home_errors_and_final_validation(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        home = root / "console-home"
        home.mkdir()
        write_config(workspace / CONFIG, {})
        config = home / "config.yaml"
        for state in (
            "malformed",
            "unreadable",
            "invalid",
            "invalid override",
            "relative home",
            "empty home",
        ):
            if config.is_dir():
                config.rmdir()
            elif config.exists():
                config.unlink()
            environment = os.environ | {"MCP_CONSOLE_HOME": str(home)}
            overrides = ()
            if state == "malformed":
                config.write_text("sandbox: [", encoding="utf-8")
            elif state == "unreadable":
                config.mkdir()
            elif state == "invalid":
                write_config(config, {"unknown": "home"})
            elif state == "invalid override":
                write_config(config, {})
                overrides = ("-c", "cache=invalid")
            else:
                environment["MCP_CONSOLE_HOME"] = (
                    "relative" if state == "relative home" else ""
                )
            for command in ("serve", "sandbox"):
                arguments = (
                    ("serve", "--no-project-config", "--no-sandbox")
                    if command == "serve"
                    else ("--no-project-config", "sandbox")
                )
                tail = () if command == "serve" else ("--", "unused-command")
                result = subprocess.run(
                    [binary, *arguments, *overrides, *tail],
                    cwd=workspace,
                    env=environment,
                    input="",
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert result.returncode == 1 and result.stdout == "", result
                if state in ("relative home", "empty home"):
                    assert (
                        "MCP_CONSOLE_HOME must be an absolute path" in result.stderr
                    ), result.stderr
                elif state == "invalid override":
                    assert (
                        "configuration with CLI overrides: cache:" in result.stderr
                    ), result.stderr
                else:
                    assert str(config) in result.stderr, result.stderr
                records.append(
                    {
                        "command": command,
                        "home": state,
                        "stderr": result.stderr.replace(str(root), "<root>"),
                    }
                )
    return records


def test_rejects_discovery_control_with_captured_settings(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = os.environ | {"MCP_CONSOLE_HOME": str(root / "home")}
        environment.pop("MCP_CONSOLE_TEST_CAPTURED", None)
        for selector in ("--config-env", "--settings-env"):
            for payload in (None, "not JSON"):
                selected_environment = environment.copy()
                if payload is not None:
                    selected_environment["MCP_CONSOLE_TEST_CAPTURED"] = payload
                for placement in ("before", "after"):
                    arguments = (
                        ("--no-project-config", "sandbox")
                        if placement == "before"
                        else ("sandbox", "--no-project-config")
                    )
                    result = subprocess.run(
                        [
                            binary,
                            *arguments,
                            selector,
                            "MCP_CONSOLE_TEST_CAPTURED",
                            "--",
                            "unused-command",
                        ],
                        cwd=root,
                        env=selected_environment,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    assert result.returncode == 1 and result.stdout == "", result
                    assert (
                        result.stderr
                        == "--no-project-config cannot be combined with --config-env or --settings-env\n"
                    ), result
                    records.append(
                        {
                            "selector": selector,
                            "flag": placement,
                            "payload": payload,
                            "stderr": result.stderr,
                        }
                    )
    return records


if __name__ == "__main__":
    run_this_suite(__file__)
