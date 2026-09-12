#!/usr/bin/env -S uv run --script

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text, last_tool_text
from support.client import McpClient
from support.normalization import code
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import WORKER, SANDBOX, command, requires
from support.execution import DIRECT, SANDBOXED, executions
from support.resolvers import (
    recording_ir_environment,
    recording_uv_environment,
    ir_run_records,
    ir_requirements,
    uv_tool_run_requirements,
    send_and_collect_runtime_python_resolution,
    resolve_public_python_version,
)
from support.ssh import SSH, configure, localhost, remote_command, poison_controller
from support.suites import run_this_suite


@requires(SSH, WORKER)
def test_discovers_remote_capability_without_preparing(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        local, remote = root / "controller", root / "remote"
        local.mkdir()
        remote.mkdir()
        remote_bin = remote / "bin"
        remote_bin.mkdir()
        marker = remote / "installer-called"
        ir = remote_bin / "ir"
        ir.write_text(
            "#!/bin/sh\nprintf called >> " + shlex.quote(str(marker)) + "\nexit 93\n"
        )
        ir.chmod(0o755)
        r_environment, _ = r_test_environment()
        prefix = remote_command(
            remote,
            binary,
            {
                "PATH": str(remote_bin),
                "HOME": str(remote),
                "R_HOME": r_environment["R_HOME"],
                "IR_CACHE_DIR": str(remote / "ir-cache"),
                "UV_CACHE_DIR": str(remote / "uv-cache"),
            },
        )
        configure(
            local, remote, prefix, sandbox={"environment": {"PATH": "/workload-only"}}
        )
        with localhost(root / "sshd") as environment:
            trap = poison_controller(root / "sshd", environment)
            with McpClient(
                binary, ("serve", "--no-sandbox"), environment, local
            ) as client:
                client.initialize_and_list_tools()
                schema = client.transcript[-1]["result"]["tools"][0]["inputSchema"]
                assert "requirements" in schema["properties"], schema
                client.send()
                assert last_result_text(client) == "\n[idle]"
                client.send(r="must_not_run <- TRUE", requirements={"r": [""]})
                assert client.transcript[-1]["result"]["isError"] is True
                client.finish()
            assert not trap.exists(), "controller discovered an execution runtime"
            assert not marker.exists(), (
                "discovery, poll, or validation invoked installation"
            )
            assert not (remote / "ir-cache").exists()
            assert not (remote / "uv-cache").exists()
            assert not (root / "sshd/controller-ir").exists()
            assert not (root / "sshd/controller-uv").exists()
        return [
            {
                "remote_managed_schema": True,
                "preparation_is_lazy": True,
                "controller_runtime_unused": True,
            }
        ]


@contextmanager
def managed_session(
    binary, execution, *, selected_python=False, inherit=True, failure_output=None
):
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        local, remote = root / "controller", root / "remote"
        local.mkdir()
        remote.mkdir()
        r_environment, ir_record = recording_ir_environment(
            remote,
            fail_requirement="console.test.failure",
            failure_output=failure_output,
        )
        uv_environment, uv_record = recording_uv_environment(remote)
        # These are trusted execution-host settings supplied by the SSH account's
        # command prefix, never by the controller environment or worker policy.
        environment = {
            name: value
            for name, value in r_environment.items()
            if name
            in {
                "HOME",
                "PATH",
                "R_HOME",
                "R_LIBS",
                "R_LIBS_USER",
                "R_LIBS_SITE",
                "R_PROFILE_USER",
                "RENV_PATHS_CACHE",
                "IR_CACHE_DIR",
            }
            or name.startswith("MCP_CONSOLE_TEST_")
        }
        environment.update(
            {
                name: value
                for name, value in uv_environment.items()
                if name == "RETICULATE_UV" or name.startswith("MCP_CONSOLE_TEST_")
            }
        )
        # Distinct host pathnames can share already downloaded artifacts in this
        # localhost harness. The controller process is forbidden to use either.
        for tool, variable in (("ir", "IR_CACHE_DIR"), ("uv", "UV_CACHE_DIR")):
            cache = Path(
                subprocess.check_output([tool, "cache", "dir"], text=True).strip()
            )
            cache.mkdir(parents=True, exist_ok=True)
            remote_cache = remote / f"{tool}-cache"
            remote_cache.symlink_to(cache, target_is_directory=True)
            environment[variable] = str(remote_cache)
        environment["UV_OFFLINE"] = "1"
        environment["UV_NO_CACHE"] = "1"
        environment["TMPDIR"] = str(remote)
        environment["R_LIBS"] = ""
        environment["R_LIBS_USER"] = str(remote / "ambient-library")
        environment["R_LIBS_SITE"] = str(remote / "ambient-library")
        override = {
            "R_HOME": environment["R_HOME"],
            "R_LIBS": "/workload-must-not-replace-managed-library",
            "MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION": "0",
            "MCP_CONSOLE_MANAGED_PYTHON": "must-not-activate",
            "MCP_CONSOLE_PREINSTALLED": "1",
            "RETICULATE_USE_MANAGED_VENV": "no",
        }
        if selected_python:
            override["RETICULATE_PYTHON"] = sys.executable
        configure(
            local,
            remote,
            remote_command(remote, binary, environment),
            extends=":workspace",
            sandbox={"inherit_environment": inherit, "environment": override},
        )
        with localhost(root / "sshd") as controller:
            trap = poison_controller(root / "sshd", controller)
            with McpClient(
                binary, execution.serve(), controller, local, response_timeout=180
            ) as client:
                client.initialize_and_list_tools()
                assert not ir_run_records(ir_record)
                assert not uv_tool_run_requirements(uv_record)
                yield client, remote, ir_record, uv_record
            assert not trap.exists()
            assert not (root / "sshd/controller-ir").exists()
            assert not (root / "sshd/controller-uv").exists()


@requires(SSH, WORKER, command("ir"), command("uv"))
@executions(DIRECT, SANDBOXED)
def test_managed_requirements_and_callbacks(binary, execution) -> Transcript:
    with managed_session(binary, execution) as (client, remote, ir_record, uv_record):
        client.send(
            requirements={"r": ["praise"], "python": ["humanize"], "duckdb": ["inet"]}
        )
        assert last_tool_text(client) == "[prepared]"
        baseline_r, baseline_python = (
            len(ir_run_records(ir_record)),
            len(uv_tool_run_requirements(uv_record)),
        )
        client.send(
            requirements={"r": ["praise"], "python": ["humanize"], "duckdb": ["inet"]}
        )
        assert last_tool_text(client) == "[prepared]"
        assert len(ir_run_records(ir_record)) == baseline_r
        assert len(uv_tool_run_requirements(uv_record)) == baseline_python
        output = send_and_collect_runtime_python_resolution(
            client,
            r=code("""
            x <- 42L
            stopifnot(requireNamespace("praise", quietly = TRUE), requireNamespace("tidyverse", quietly = TRUE))
            Sys.setenv(RETICULATE_UV = "/worker-must-not-select-uv", IR_CACHE_DIR = "/worker-must-not-select-cache")
            library(zeallot)
            cat("R ready:", x, "\\n")
            """),
        )
        assert "R ready: 42" in output, output
        assert any(
            "zeallot" in ir_requirements(entry) for entry in ir_run_records(ir_record)
        )
        output = send_and_collect_runtime_python_resolution(
            client,
            python=code("""
            import humanize, pyfiglet
            print(humanize.intcomma(12345))
            print(r.x)
            """),
        )
        assert "12,345" in output and "42" in output, output
        assert any(
            "pyfiglet" in packages for packages in uv_tool_run_requirements(uv_record)
        )
        output = send_and_collect_runtime_python_resolution(
            client,
            r='reticulate::py_require("more-itertools"); reticulate::py_run_string("import more_itertools; print(list(more_itertools.take(3, range(10))))")',
        )
        assert "[0, 1, 2]" in output, output
        selected = resolve_public_python_version(client, [">=3.11"])
        assert not client.transcript[-1]["result"].get("isError"), selected
        client.send(sql="LOAD inet")
        client.send(sql="SELECT host('127.0.0.1'::INET) AS address")
        assert "127.0.0.1" in last_tool_text(client), last_tool_text(client)
        client.send(
            control="restart", requirements={"r": ["praise"], "python": ["humanize"]}
        )
        assert not client.transcript[-1]["result"].get("isError"), last_result_text(
            client
        )
        output = send_and_collect_runtime_python_resolution(
            client, r='stopifnot(!exists("x")); library(zeallot); cat("reused\\n")'
        )
        assert output == "reused\n", output
        client.send(python='import pyfiglet, humanize, more_itertools; print("reused")')
        assert last_tool_text(client) == "reused\n"
        records = [
            json.loads(line)
            for line in (remote / "uv-environment.jsonl").read_text().splitlines()
        ]
        assert all(
            entry["UV_OFFLINE"] is None
            and entry["UV_CACHE_DIR"] == str(remote / "uv-cache")
            for entry in records
        ), records
        transcript = client.finish()
        return json.loads(
            json.dumps(transcript[3:]).replace(str(remote.parent), "<ssh-test>")
        )


@requires(SSH, WORKER, command("ir"), command("uv"))
@executions(DIRECT, SANDBOXED)
def test_selected_python_preserves_managed_r_without_inheritance(
    binary, execution
) -> Transcript:
    with managed_session(binary, execution, selected_python=True, inherit=False) as (
        client,
        remote,
        ir_record,
        uv_record,
    ):
        client.send(requirements={"r": ["praise"]})
        assert last_tool_text(client) == "[prepared]"
        assert not uv_tool_run_requirements(uv_record), (
            "selected Python invoked managed Python"
        )
        send_and_collect_runtime_python_resolution(
            client,
            r='x <- 42L; stopifnot(requireNamespace("praise", quietly = TRUE)); x',
        )
        assert last_tool_text(client) == "[1] 42\n", last_tool_text(client)
        client.send(
            requirements={"python": ["humanize"]}, control="restart", r="x <- 0L"
        )
        assert "user-selected Python" in last_result_text(client)
        client.send(r="x")
        assert last_tool_text(client) == "[1] 42\n", last_tool_text(client)
        send_and_collect_runtime_python_resolution(
            client, python="import sys; print(sys.executable)"
        )
        assert last_tool_text(client).strip() == sys.executable
        return json.loads(
            json.dumps(client.finish()[3:])
            .replace(str(remote.parent), "<ssh-test>")
            .replace(sys.executable, "<selected-remote-python>")
        )


@requires(SSH, WORKER, command("ir"), command("uv"))
@executions(DIRECT, SANDBOXED)
def test_failed_restart_and_invalid_requirements_preserve_worker(binary, execution):
    with managed_session(binary, execution) as (client, remote, ir_record, uv_record):
        send_and_collect_runtime_python_resolution(
            client, r="sentinel <- 42L; worker <- Sys.getpid()"
        )
        assert last_tool_text(client) == "[done]"
        counts = (
            len(ir_run_records(ir_record)),
            len(uv_tool_run_requirements(uv_record)),
        )
        for requirements in (
            {"python": ["/tmp/untrusted"]},
            {"duckdb": ["json; SELECT 1"]},
            {"r": ["praise\n"]},
        ):
            client.send(r="sentinel <- 0L", requirements=requirements)
            assert client.transcript[-1]["result"]["isError"]
        client.send(
            r=code(r"""
                options(useFancyQuotes = FALSE)
                tryCatch(library("../untrusted"), error = function(e) cat(conditionMessage(e), "\n"))
                """)
        )
        assert counts == (
            len(ir_run_records(ir_record)),
            len(uv_tool_run_requirements(uv_record)),
        )
        client.send(
            control="restart",
            r="sentinel <- 0L",
            requirements={"r": ["console.test.failure"]},
        )
        assert "synthetic `ir` failure" in last_result_text(client), last_result_text(
            client
        )
        client.send(r="stopifnot(Sys.getpid() == worker); sentinel")
        assert last_tool_text(client) == "[1] 42\n"
        client.send(requirements={"r": ["praise"]}, r="sentinel")
        assert last_tool_text(client) == "[1] 42\n"
        return json.loads(
            json.dumps(client.finish()[3:]).replace(str(remote.parent), "<ssh-test>")
        )


@requires(SSH, WORKER, command("ir"), command("uv"))
@executions(DIRECT, SANDBOXED)
def test_large_remote_install_failure_preserves_diagnostics_and_worker(
    binary, execution
):
    diagnostic = ('compile: α\t"error"\\source ' * 128).rstrip()
    diagnostics = ((diagnostic + "\n") * 352) + "final diagnostic"
    with managed_session(binary, execution, failure_output=diagnostics) as (
        client,
        remote,
        ir_record,
        uv_record,
    ):
        send_and_collect_runtime_python_resolution(
            client, r="sentinel <- 42L; worker <- Sys.getpid()"
        )
        client.send(
            control="restart",
            r="sentinel <- 0L",
            requirements={"r": ["console.test.failure"]},
        )
        assert client.transcript[-1]["result"]["isError"]
        output = last_result_text(client)
        expected = f"[R package resolution failed with exit status: 1: {diagnostics}]"
        assert output == expected, {"length": len(output), "prefix": output[:500]}
        client.send(r="stopifnot(Sys.getpid() == worker); sentinel")
        assert last_tool_text(client) == "[1] 42\n"
        client.send(requirements={"r": ["praise"]}, r="sentinel")
        assert last_tool_text(client) == "[1] 42\n"
        return client.finish()[3:]


@requires(SSH, WORKER, command("ir"), command("uv"))
@executions(DIRECT, SANDBOXED)
def test_failed_remote_activation_preserves_worker_until_restart(binary, execution):
    with managed_session(binary, execution) as (client, remote, ir_record, uv_record):
        send_and_collect_runtime_python_resolution(
            client, r="sentinel <- 42L; worker <- Sys.getpid()"
        )
        baseline = len(ir_run_records(ir_record))
        send_and_collect_runtime_python_resolution(
            client,
            r=code(r"""
            local({
              invisible(suppressMessages(trace(
                ".libPaths",
                tracer = quote(if (!missing(new)) stop("remote activation failed")),
                print = FALSE, where = baseenv()
              )))
              on.exit(invisible(suppressMessages(untrace(".libPaths", where = baseenv()))))
              do.call(loadNamespace, list(package = "zeallot"))
            })
            """),
        )
        assert last_tool_text(client) == "Error: remote activation failed\n", (
            last_tool_text(client)
        )
        runs = ir_run_records(ir_record)
        assert len(runs) == baseline + 1, {
            "baseline": baseline,
            "requirements": [ir_requirements(run) for run in runs],
        }
        client.send(r="stopifnot(Sys.getpid() == worker); sentinel")
        assert last_tool_text(client) == "[1] 42\n"
        client.send(requirements={"r": ["praise"]})
        assert last_tool_text(client) == "[restart required]"
        client.send(control="restart")
        client.send(
            r='stopifnot(!exists("sentinel"), requireNamespace("praise", quietly = TRUE)); 42L'
        )
        assert last_tool_text(client) == "[1] 42\n"
        return client.finish()[3:]


@requires(SSH, WORKER, SANDBOX, command("ir"), command("uv"))
def test_worker_network_stays_denied_during_remote_preparation(binary):
    with (
        managed_session(binary, SANDBOXED) as (client, remote, ir_record, uv_record),
        socket.socket() as listener,
    ):
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        network = code(f"""
            import socket
            try:
                with socket.socket() as connection:
                    assert connection.connect_ex(("127.0.0.1", {port})) != 0
            except PermissionError:
                pass
            print("worker network denied")
            """)
        send_and_collect_runtime_python_resolution(client, python=network)
        assert last_tool_text(client) == "worker network denied\n", last_tool_text(
            client
        )
        output = send_and_collect_runtime_python_resolution(
            client, python='import pyfiglet; print("addition available")'
        )
        assert output == "addition available\n", output
        assert any(
            "pyfiglet" in packages for packages in uv_tool_run_requirements(uv_record)
        )
        send_and_collect_runtime_python_resolution(client, python=network)
        assert last_tool_text(client) == "worker network denied\n", last_tool_text(
            client
        )
        transcript = client.finish()[3:]
        for entry in transcript:
            if "send" in entry and entry["send"].get("python") == network:
                entry["send"]["python"] = network.replace(str(port), "<listening-port>")
        return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
