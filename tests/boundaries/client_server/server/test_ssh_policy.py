#!/usr/bin/env -S uv run --script

import json
import os
import re
import shlex
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
from support.ssh import (
    CONFIG,
    SSH,
    EXTERNAL_SSH,
    configure,
    localhost,
    poison_controller,
    remote_command,
)
from support.suites import run_this_suite


EXERCISE = code(r"""
    writable <- function(path) {
      tryCatch({writeLines("remote value", path); TRUE}, error = function(error) FALSE)
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
                client.send(r=EXERCISE)
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
                client.send(r="must_not_run <- TRUE")
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


@requires(EXTERNAL_SSH)
def test_external_execution_host_policy(binary: Path) -> Transcript:
    # Test infrastructure provisions the host and existing workspace. No uploads,
    # installation, directory creation, or synchronization happen in the server.
    external = json.loads(os.environ["MCP_CONSOLE_TEST_SSH_EXTERNAL"])
    with TemporaryDirectory() as temporary:
        local = Path(temporary)
        config = local / CONFIG
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "target": external["target"],
                    "sandbox": policy(external["environment"]),
                }
            )
        )
        ssh = local / "ssh"
        ssh.write_text(
            code(r"""
                #!/bin/sh
                exec /usr/bin/ssh -F CONFIG "$@"
                """).replace("CONFIG", shlex.quote(external["ssh_config"]))
        )
        ssh.chmod(0o755)
        environment = {
            **os.environ,
            "PATH": str(local),
            "R_HOME": "/unavailable-controller-R",
        }
        with McpClient(
            binary, ("serve", "--writable-root", "cli"), environment, local
        ) as client:
            client.initialize_and_list_tools()
            client.send(
                r="stopifnot(Sys.info()[['sysname']] == "
                + json.dumps(external["platform"])
                + "); "
                + EXERCISE
            )
            assert (
                last_result_text(client) == "remote relative and CLI grants verified\n"
            ), last_result_text(client)
            return client.finish()[3:]


if __name__ == "__main__":
    run_this_suite(__file__)
