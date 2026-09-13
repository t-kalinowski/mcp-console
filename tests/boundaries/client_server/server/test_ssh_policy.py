#!/usr/bin/env -S uv run --script

import json
import re
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.client import McpClient
from support.normalization import code
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import SANDBOX, WORKER, requires
from support.resolvers import send_and_collect_runtime_python_resolution
from support.ssh import (
    CONFIG,
    SSH,
    configure,
    client_environment,
    localhost,
    poison_controller,
    remote_command,
)
from support.ssh_external import EXTERNAL_SSH, external_target
from support.suites import run_this_suite


# fmt: r
EXERCISE = code(r"""
    writable <- function(path) {
      tryCatch(
        {
          writeLines("remote value", path)
          TRUE
        },
        error = function(error) FALSE
      )
    }
    suppressWarnings(stopifnot(
      writable("results/value"),
      writable("cli/value"),
      !writable("denied/value"),
      !writable("workspace-value")
    ))
    cat("remote relative and CLI grants verified\n")
    """)


def policy(environment: dict[str, str]) -> dict:
    return {
        "environment": environment,
        "filesystem": {
            "entries": [
                {"path": {"type": "path", "path": "results"}, "access": "write"},
            ]
        },
    }


@requires(SSH, WORKER, SANDBOX)
def test_policy_uses_remote_paths_and_native_validation(binary: Path) -> Transcript:
    r_environment, _ = r_test_environment()
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        local, remote = root / "local", root / "remote"
        for directory in (
            local,
            remote,
            remote / "results",
            remote / "cli",
            remote / "denied",
        ):
            directory.mkdir()
        selected = policy({"R_HOME": r_environment["R_HOME"]})
        config = configure(local, remote, [str(binary)], sandbox=selected)
        configure(
            remote,
            remote,
            ["unused"],
            sandbox={"filesystem": {"kind": "unrestricted"}, "network": "enabled"},
        )
        with localhost(root / "sshd") as environment:
            with McpClient(
                binary, ("serve", "--writable-root", "cli"), environment, local
            ) as client:
                client.initialize_and_list_tools()
                send_and_collect_runtime_python_resolution(client, r=EXERCISE)
                assert (
                    last_result_text(client)
                    == "remote relative and CLI grants verified\n"
                ), last_result_text(client)
                transcript = client.finish()[3:]
            assert (remote / "results/value").read_text() == "remote value\n"
            assert not (local / "results").exists()
            selected["network"] = "invalid-native-mode"
            configure(local, remote, [str(binary)], sandbox=selected)
            with McpClient(binary, ("serve",), environment, local) as client:
                client.initialize_and_list_tools()
                send_and_collect_runtime_python_resolution(
                    client, r="must_not_run <- TRUE"
                )
                assert "remote launcher exited" in last_result_text(client), (
                    last_result_text(client)
                )
                client.stdin.close()
                client.stdout.read(timeout=12)
                stderr = client.stderr.read(timeout=12)
                client.process.wait(timeout=12)
                assert "mcp-console-sandbox: invalid configuration JSON" in stderr, (
                    stderr
                )
                stderr = re.sub(
                    r"(at line [0-9]+, column )[0-9]+", r"\1<column>", stderr
                )
                transcript.extend(client.transcript[3:])
                transcript.append({"standard_error": stderr})
        # The same target has no effect on the public standalone sandbox.
        configure(local, remote, ["must-not-launch"], extends=":workspace")
        result = subprocess.run(
            [binary, "sandbox", "--", "/bin/pwd"],
            cwd=local,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0 and result.stdout.strip() == str(local), result
        return transcript + [{"standalone_sandbox_remained_local": True}]


@requires(SSH, WORKER, SANDBOX)
def test_invalid_environment_reaches_remote_native_validation(
    binary: Path,
) -> Transcript:
    r_environment, _ = r_test_environment()
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        local, remote = root / "local", root / "remote"
        local.mkdir()
        remote.mkdir()
        prefix = remote_command(
            remote,
            binary,
            {
                "PATH": "/usr/bin:/bin",
                "R_HOME": r_environment["R_HOME"],
                "R_LIBS_USER": "/unavailable",
                "R_LIBS_SITE": "/unavailable",
            },
        )
        transcript = []
        with localhost(root / "sshd") as environment:
            trap = poison_controller(root / "sshd", environment)
            for inherit in (True, False):
                for invalid in ([], {"R_HOME": 42}, {"RETICULATE_PYTHON": 42}):
                    configure(
                        local,
                        remote,
                        prefix,
                        sandbox={
                            "inherit_environment": inherit,
                            "environment": invalid,
                        },
                    )
                    with McpClient(binary, ("serve",), environment, local) as client:
                        client.initialize_and_list_tools()
                        client.send(r="must_not_run <- TRUE")
                        assert "remote launcher exited" in last_result_text(client), (
                            last_result_text(client)
                        )
                        client.stdin.close()
                        client.stdout.read(timeout=12)
                        stderr = client.stderr.read(timeout=12)
                        client.process.wait(timeout=12)
                        assert (
                            "mcp-console-sandbox: invalid configuration JSON" in stderr
                        ), stderr
                        stderr = re.sub(
                            r"(at line [0-9]+, column )[0-9]+", r"\1<column>", stderr
                        )
                        transcript.append(
                            {"inherit_environment": inherit, "environment": invalid}
                        )
                        transcript.extend(client.transcript[3:])
                        transcript.append({"standard_error": stderr})
            assert not trap.exists(), "controller discovered an execution runtime"
        return transcript


@requires(SSH)
def test_nul_runtime_selectors_fail_on_remote_host_without_panicking(
    binary: Path,
) -> Transcript:
    transcript = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        with localhost(root / "sshd") as environment:
            trap = poison_controller(root / "sshd", environment)
            for name in ("R_HOME", "RETICULATE_PYTHON"):
                configure(
                    root,
                    root,
                    [str(binary)],
                    sandbox={"environment": {name: "invalid\0selection"}},
                )
                with McpClient(binary, ("serve",), environment, root) as client:
                    assert client.process.wait(timeout=12) != 0
                    assert not client.stdout.read()
                    errors = client.stderr.read()
                    assert f"remote {name} selection must not contain NUL" in errors, (
                        errors
                    )
                    assert "panicked" not in errors, errors
                    transcript.append({"selection": name, "standard_error": errors})
            assert not trap.exists(), "controller discovered an execution runtime"
    return transcript


@requires(EXTERNAL_SSH)
def test_external_execution_host_policy(binary: Path) -> Transcript:
    with external_target() as external, TemporaryDirectory() as temporary:
        local = Path(temporary)
        config = local / CONFIG
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "target": external["target"],
                    "sandbox": policy(
                        {
                            **external["environment"],
                            "MCP_CONSOLE_TEST_SSH_PLATFORM": external["platform"],
                        }
                    ),
                }
            )
        )
        environment = client_environment(
            local, config=external.get("ssh_config"), remote_path=external.get("path")
        )
        trap = poison_controller(local, environment)
        with McpClient(
            binary, ("serve", "--writable-root", "cli"), environment, local
        ) as client:
            client.initialize_and_list_tools()
            send_and_collect_runtime_python_resolution(
                client,
                # fmt: r
                r=code("""
                    stopifnot(
                      Sys.info()[["sysname"]] == Sys.getenv("MCP_CONSOLE_TEST_SSH_PLATFORM")
                    )
                    """)
                + EXERCISE,
            )
            assert (
                last_result_text(client) == "remote relative and CLI grants verified\n"
            ), last_result_text(client)
            transcript = client.finish()[3:]
        assert not trap.exists(), "controller discovered an execution runtime"
        return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
