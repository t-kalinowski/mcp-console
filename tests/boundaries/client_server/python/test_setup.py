#!/usr/bin/env -S uv run --script

import json
import os
import subprocess
import shutil
import sys
import tempfile
from pathlib import Path
from contextlib import contextmanager

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import (
    assert_exact_interleaving,
    last_result_text,
    wait_for_evaluation_output,
)
from support.checkpoints import FifoCheckpoint
from support.processes import host_process_id, process_exists, stop_process_id
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code, normalize_python_resolution_error
from support.native import build_interposer
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, requires
from support.r import startup_r_package
from support.resolvers import send_and_collect_runtime_python_resolution
from support.suites import run_this_suite


@contextmanager
def startup_client(
    binary: Path,
    execution: Execution,
    source: str,
    *,
    selected_python: str | None = None,
):
    with tempfile.TemporaryDirectory() as temporary:
        modules = Path(temporary)
        (modules / "sitecustomize.py").write_text(
            "import __main__\n"
            f"exec(compile({json.dumps(source)}, '<setup checkpoint>', 'exec'), __main__.__dict__)\n"
        )
        environment = dict(os.environ, RETICULATE_PYTHONPATH=str(modules))
        if selected_python is not None:
            environment["RETICULATE_PYTHON"] = selected_python
        with McpClient(binary, execution.serve(), environment) as client:
            client.initialize_and_list_tools()
            yield client


@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_startup_hooks_use_the_native_owner_once(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        probe = build_interposer(root, "python_initialized")
        source = code(f"""
            startup_probe <- dyn.load({json.dumps(str(probe))})
            callback_calls <- attachment_calls <- 0L
            python_initialized <- function() .C(
              getNativeSymbolInfo("mcp_console_probe_python_initialized", PACKAGE = startup_probe),
              value = 0L
            )$value
            options(reticulate.python.beforeInitialized = function() {{
              callback_calls <<- callback_calls + 1L
              stopifnot(python_initialized() == -1L)
              reticulate::use_python(Sys.getenv("RETICULATE_PYTHON"), required = TRUE)
            }})
            suppressMessages(trace("py_initialize", where = asNamespace("reticulate"),
              print = FALSE, tracer = quote({{
                attachment_calls <<- attachment_calls + 1L
                stopifnot(python_initialized() == 1L)
              }})))
            # Reticulate discovery and metadata subprocesses must never select
            # or inspect another interpreter during worker startup.
            assignInNamespace("py_discover_config", function(...) stop("rediscovery"), "reticulate")
            assignInNamespace("python_config_impl", function(...) stop("metadata subprocess"), "reticulate")
            """)
        with startup_r_package(root, source) as environment:
            environment["RETICULATE_PYTHON"] = sys.executable
            with McpClient(binary, execution.serve(), environment, root) as client:
                client.initialize_and_list_tools()
                client.send(
                    r=code("""
                    stopifnot(callback_calls == 1L, attachment_calls == 1L)
                    config <- reticulate::py_config()
                    stopifnot(config$available, file.exists(config$libpython))
                    reticulate::py_run_string("owned_object = object(); owned_id = id(owned_object)")
                    cat("one native bootstrap and bridge attachment\\n")
                    """)
                )
                assert (
                    last_result_text(client)
                    == "one native bootstrap and bridge attachment\n"
                ), client.transcript[-1]
                client.send(
                    python="assert id(owned_object) == owned_id; assert int(r.callback_calls) == 1; 42"
                )
                assert last_result_text(client) == "42\n", client.transcript[-1]
                return client.finish()


@executions(DIRECT, SANDBOXED)
def test_retains_startup_failure_until_explicit_restart(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        hook = root / "startup.R"
        counter = root / "hook-count"
        source = code(f"""
            options(reticulate.python.afterInitialized = function() {{
              cat("hook\\n", file = {json.dumps(str(counter))}, append = TRUE)
              stop("synthetic startup attachment failure")
            }})
            """)
        with startup_r_package(root, source) as environment:
            serve = (
                execution.serve("--writable-root", str(root))
                if execution == SANDBOXED
                else execution.serve()
            )
            with McpClient(binary, serve, environment, root) as client:
                client.initialize_and_list_tools()
                failed = client.send(
                    python="raise AssertionError('failed startup ran code')"
                )
                assert failed.get("isError"), failed
                assert "synthetic startup attachment failure" in last_result_text(
                    client
                ), client.transcript[-1]
                assert counter.read_text() == "hook\n"
                client.request("ping")
                failed = client.send(
                    python="raise AssertionError('failed startup ran code again')"
                )
                assert failed.get("isError"), failed
                assert counter.read_text() == "hook\n", (
                    "startup hook was retried automatically"
                )
                client.send(
                    control="restart",
                    python="raise AssertionError('replacement startup ran code')",
                )
                assert counter.read_text() == "hook\nhook\n"
                result = client.send(
                    python="raise AssertionError('replacement retried startup')"
                )
                assert result.get("isError"), result
                assert counter.read_text() == "hook\nhook\n"
                hook.write_text("startup_recovered <- TRUE\n")
                client.send(control="restart")
                client.send(r="stopifnot(startup_recovered); 42L")
                assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
                return client.finish()


@executions(DIRECT, SANDBOXED)
def test_failed_preparation_preserves_initialized_runtime(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(r="startup_marker <- 41L; cat(reticulate::py_config()$python)")
        executable = last_result_text(client)
        assert Path(executable).is_file(), client.transcript[-1]
        client.transcript[-1]["result"]["content"][0]["text"] = "<running Python>"
        result = client.send(
            python="raise AssertionError('failed preparation ran the cell')",
            requirements={"python": ["more-itertools<1,>=10"]},
        )
        assert result["isError"] is True, result
        output = last_result_text(client)
        assert "No solution found" in output, client.transcript[-1]
        client.transcript[-1]["result"]["content"][0]["text"] = (
            normalize_python_resolution_error(output, executable=executable)
        )
        client.send(
            # fmt: r
            r=code("""
                stopifnot(reticulate::py_available(initialize = FALSE))
                startup_marker + 1L
                """)
        )
        assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
        client.send(python="r.startup_marker + 1", requirements={"python": ["numpy"]})
        assert last_result_text(client) == "42\n", client.transcript[-1]
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_activates_without_reticulate_virtualenv_helper(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        # fmt: r
        r = code(r"""
            invisible(reticulate::py_config())
            namespace <- asNamespace("reticulate")
            invisible(suppressMessages(base::trace(
              "py_activate_virtualenv",
              tracer = quote(stop("reticulate activation helper was called")),
              print = FALSE,
              where = namespace
            )))
            """)
        client.send(r=r)
        assert last_result_text(client) == "[done]", client.transcript[-1]
        # fmt: python
        python = code("""
            import multiprocessing.spawn
            import sys

            initial_executable = sys.executable
            initial_prefix = sys.prefix
            live_object = object()
            live_object_id = id(live_object)
            42
            """)
        client.send(python=python)
        assert last_result_text(client) == "42\n", client.transcript[-1]
        # fmt: r
        r = code(r"""
            reticulate::py_require("py-yaml12")
            config <- reticulate::py_config()
            sys <- reticulate::import("sys", convert = FALSE)
            stopifnot(
              identical(config$executable, reticulate::py_to_r(sys$executable)),
              identical(config$prefix, reticulate::py_to_r(sys$prefix)),
              isTRUE(config$available),
              isTRUE(config$ephemeral)
            )
            42L
            """)
        output = send_and_collect_runtime_python_resolution(client, r=r, timeout_ms=0)
        assert output == "[1] 42\n", client.transcript[-1]
        # fmt: python
        python = code("""
            import subprocess
            import yaml12

            assert id(live_object) == live_object_id
            assert sys.executable != initial_executable
            assert sys.prefix != initial_prefix
            child_executable = subprocess.check_output(
                [sys.executable, "-c", "import sys, yaml12; print(sys.executable)"],
                text=True,
            ).strip()
            assert child_executable == sys.executable
            with multiprocessing.get_context("spawn").Pool(1) as pool:
                child_executable = pool.apply(eval, ("__import__('sys').executable",))
            assert child_executable == sys.executable
            42
            """)
        client.send(python=python)
        assert last_result_text(client) == "42\n", client.transcript[-1]
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_preserves_setup_after_r_initialization(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(
            # fmt: r
            r=code("""
                invisible(reticulate::py_config())
                """)
        )
        assert last_result_text(client) == "[done]", client.transcript[-1]
        # Load multiprocessing before activation so a spawned child must use
        # the updated interpreter after the new requirements are available.
        # fmt: python
        python = code("""
            import multiprocessing.spawn
            import sys

            initial_executable = sys.executable
            42
            """)
        client.send(python=python)
        assert last_result_text(client) == "42\n", client.transcript[-1]
        # fmt: r
        r = code(r"""
            reticulate::py_require(c("matplotlib", "py-yaml12"))
            plt <- reticulate::import("matplotlib.pyplot")
            invisible(plt$show())
            42L
            """)
        output = send_and_collect_runtime_python_resolution(client, r=r, timeout_ms=0)
        assert output == "[1] 42\n", client.transcript[-1]
        # fmt: python
        python = code("""
            import subprocess

            import matplotlib.pyplot as plt

            assert sys.executable != initial_executable
            child_executable = subprocess.check_output(
                [sys.executable, "-c", "import sys, yaml12; print(sys.executable)"],
                text=True,
            ).strip()
            assert child_executable == sys.executable
            with multiprocessing.get_context("spawn").Pool(1) as pool:
                child_executable = pool.apply(eval, ("__import__('sys').executable",))
            assert child_executable == sys.executable
            plt.show()
            42
            """)
        client.send(python=python)
        assert last_result_text(client) == "42\n", client.transcript[-1]
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_rejects_startup_environment_mutation(
    binary: Path, execution: Execution
) -> Transcript:
    source = "import sys; sys.prefix = 'changed-by-startup-hook'"
    with startup_client(
        binary, execution, source, selected_python=sys.executable
    ) as client:
        client.send(python="raise AssertionError('invalid environment ran code')")
        result = client.transcript[-1]["result"]
        output = last_result_text(client)
        assert result["isError"] is True, result
        traceback = (
            "Traceback (most recent call last):\n"
            '  File "<string>", line 709, in _mcp_console_configure_environment\n'
            "RuntimeError: embedded Python prefix differs from the selected environment: "
            f"'changed-by-startup-hook' != {sys.prefix!r}\n"
        )
        failure = "Python environment setup failed; restart required\n"
        lifecycle = (
            "[worker sideband read failed: worker sideband closed]\n"
            "[worker exited with status 1]\n"
            "[worker stopped: in-memory state lost]"
        )
        assert output.endswith(lifecycle), output
        # The Python diagnostic and fatal stderr have independent transports.
        assert_exact_interleaving(output[: -len(lifecycle)], traceback, failure)
        result["content"][0]["text"] = (traceback + failure + lifecycle).replace(
            repr(sys.prefix), "'<selected Python prefix>'"
        )
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
