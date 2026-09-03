#!/usr/bin/env -S uv run --script

import json
import os
import plistlib
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _support import (
    FifoCheckpoint,
    McpClient,
    Transcript,
    assert_result_content,
    checkpoint_uv_environment,
    code,
    r_test_environment,
    reference_plots,
    release_worker_callback_gate,
    run_this_suite,
    stop_client,
    wait_for_evaluation_output,
    wait_for_idle_output,
    wait_for_worker_file,
)

PLATFORMS = {"darwin"}
PYTHON_DOWNLOAD_URL = "https://example.invalid/python.tar.zst"


from client_server._harness import (
    _zod_last_tool_text as last_tool_text,
    matplotlib_test_environment,
    named_requirement_error,
)


def test_prepares_initial_python_requirements(binary: Path) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    client.send(
        requirements={"python": ["py-yaml12"]},
    )
    assert last_tool_text(client) == "[prepared]"
    invalid = "not a valid requirement !!!"

    client.send(
        requirements={"python": [invalid]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    recorded_error = named_requirement_error(invalid)
    assert result["content"][0]["text"] == recorded_error
    client.send(
        requirements={"python": [invalid]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == recorded_error
    client.send(
        requirements={"python": ["numpy\npandas"]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == named_requirement_error("numpy\npandas")
    # fmt: r
    r = code(r"""
        seed <- tail(reticulate::py_require()$history, 1L)[[1L]]
        printed_requirements <- capture.output(print(reticulate::py_require()))
        stopifnot(
          identical(seed$requested_from, "mcp-console"),
          identical(seed$action, "set"),
          isFALSE(seed$exclude_newer_supplied),
          identical(seed$packages, c("numpy", "pandas", "py-yaml12")),
          length(printed_requirements) > 0L
        )
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"
    # fmt: python
    python = code("""
        import yaml12

        yaml12.__name__
        """)
    client.send(python=python)
    assert last_tool_text(client) == "'yaml12'\n"
    client.send(
        requirements={"python": ["py-yaml12"]},
    )
    assert last_tool_text(client) == "[prepared]"
    return client._finish()


def test_prepares_explicit_numpy_requirement(binary: Path) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    client.send(
        requirements={"python": ["numpy"]},
    )
    assert last_tool_text(client) == "[prepared]"
    # fmt: r
    r = code(r"""
        stopifnot(Sys.getenv("RETICULATE_PYTHON") == "managed")
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"
    return client._finish()


def test_does_not_fail_resolution_when_matplotlib_cache_cannot_be_written(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        environment = matplotlib_test_environment(temporary / "host-cache")
        cache_directory = temporary / "user-matplotlib"
        environment["MPLCONFIGDIR"] = str(cache_directory)
        environment["MPL_IGNORE_SYSTEM_FONTS"] = "1"
        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=temporary,
        )
        client._initialize_and_list_tools()
        client.send(
            requirements={"python": ["matplotlib"]},
        )
        assert last_tool_text(client) == "[prepared]"
        caches = list(cache_directory.glob("fontlist-v*.json"))
        assert len(caches) == 1, caches
        caches[0].unlink()
        caches[0].mkdir()

        client.send(
            requirements={"python": ["py-yaml12"]},
        )
        assert last_tool_text(client) == "[prepared]", client.transcript[-1]
        assert caches[0].is_dir()
        assert not [
            path for path in cache_directory.glob("fontlist-v*.json") if path.is_file()
        ]
        client.send(
            python="(__import__('matplotlib').__name__, __import__('yaml12').__name__)"
        )
        assert last_tool_text(client) == "('matplotlib', 'yaml12')\n"
        return client._finish()


def test_restart_loses_state_and_retains_python_requirements(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.send(
        requirements={"python": ["py-yaml12"]},
    )
    assert last_tool_text(client) == "[prepared]"
    client.send(python="restart_marker = 42")
    assert last_tool_text(client) == "[done]"

    client.send(control="restart")
    assert last_tool_text(client) == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
    )

    # fmt: python
    python = code("""
        import yaml12

        "restart_marker" in globals(), yaml12.__name__
        """)
    client.send(python=python)
    assert last_tool_text(client) == "(False, 'yaml12')\n"
    return client._finish()


def test_restart_discards_pre_marker_python_activation(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        replacement_requirement = "mcp-console-restart-fixture"
        environment, uv_started, uv_release = checkpoint_uv_environment(
            temporary,
            replacement_requirement,
            reuse_resolved_python_for=("py-yaml12", replacement_requirement),
        )
        environment["TMPDIR"] = temporary_directory
        reuse_record = Path(environment["MCP_CONSOLE_TEST_UV_REUSE_RECORD"])

        client = McpClient(binary, ("serve",), environment)
        passed = False
        worker_checkpoints: list[FifoCheckpoint] = []
        try:
            client._initialize_and_list_tools()
            # fmt: r
            r = code(r"""
                config <- reticulate::py_config()
                activation_ready <- tempfile("mcp-console-activation-ready-")
                activation_release <- tempfile("mcp-console-activation-release-")
                activation_sent <- tempfile("mcp-console-activation-sent-")
                cat(
                  activation_ready,
                  activation_release,
                  activation_sent,
                  config$python,
                  sep = "\n"
                )
                """)
            client.send(r=r)
            setup = client.transcript[-1]["result"]
            paths = setup["content"][0]["text"].splitlines()
            assert len(paths) == 4, setup
            resolved_python = paths.pop()
            assert Path(resolved_python).is_file(), resolved_python
            Path(environment["MCP_CONSOLE_TEST_UV_REUSE_PYTHON"]).write_text(
                resolved_python,
                encoding="utf-8",
            )
            setup["content"][0]["text"] = (
                "<activation ready>\n<activation release>\n<activation sent>"
            )
            activation_ready, activation_release, activation_sent = [
                FifoCheckpoint(Path(path)) for path in paths
            ]
            worker_checkpoints.extend(
                (activation_ready, activation_release, activation_sent)
            )

            # Pause the real managed worker after its new environment resolves,
            # immediately before its active binding publishes python_activated.
            # fmt: r
            r = code(r"""
                globals <- get(".globals", envir = asNamespace("reticulate"))
                original <- activeBindingFunction("python_requirements", globals)
                rm(list = "python_requirements", envir = globals)
                makeActiveBinding("python_requirements", function(value) {
                  if (missing(value)) {
                    return(original())
                  }
                  ready <- fifo(activation_ready, open = "wb", blocking = TRUE)
                  writeBin(charToRaw("1"), ready)
                  close(ready)
                  release <- fifo(activation_release, open = "rb", blocking = TRUE)
                  stopifnot(identical(readBin(release, "raw", n = 1L), charToRaw("1")))
                  close(release)
                  original(value)
                  sent <- fifo(activation_sent, open = "wb", blocking = TRUE)
                  writeBin(charToRaw("1"), sent)
                  close(sent)
                }, globals)
                reticulate::py_require("py-yaml12")
                """)
            evaluation = client._start_send(r=r, timeout_ms=0)
            activation_ready.wait("managed Python activation")
            client._receive(evaluation)
            evaluation_result = evaluation["result"]
            assert evaluation_result == {
                "content": [
                    {
                        "type": "text",
                        "text": "\n[running; poll with an empty send]",
                    }
                ],
                "isError": False,
            }, evaluation_result

            restart = client._start_send(
                control="restart",
                requirements={"python": [replacement_requirement]},
            )
            uv_started.wait("restart Python resolution")
            activation_release.release()
            activation_sent.wait("published managed Python activation")
            uv_release.release()
            client._receive(restart)

            restart_result = restart["result"]
            assert restart_result.get("isError") is not True, restart_result
            assert restart_result["content"] == [
                {
                    "type": "text",
                    "text": (
                        "[active evaluation stopped by session restart request]\n"
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n"
                        "[idle]"
                    ),
                }
            ], restart_result

            # The replacement environment wins over the old generation's
            # activation, even though that event preceded ordered retirement.
            # fmt: r
            r = code(f"""
                packages <- reticulate::py_require()$packages
                c("{replacement_requirement}" %in% packages, "py-yaml12" %in% packages)
                """)
            client.send(r=r)
            assert last_tool_text(client) == "[1]  TRUE FALSE\n"
            assert reuse_record.read_text(encoding="utf-8").splitlines() == [
                "py-yaml12",
                replacement_requirement,
            ]
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_client(client)
            for checkpoint in worker_checkpoints:
                checkpoint.close()
            uv_started.close()
            uv_release.close()


def test_prepares_python_requirements_after_worker_startup(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    python = code("""
        import importlib.util; import os; import sys
        sentinel = 42; worker_pid = os.getpid(); initial_prefix = sys.prefix
        importlib.util.find_spec("yaml12") is None
        """)
    client.send(python=python)
    assert last_tool_text(client) == "True\n"

    invalid = "not a valid requirement !!!"
    client.send(
        requirements={"python": [invalid]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == named_requirement_error(invalid)

    python = code("""
        sentinel, os.getpid() == worker_pid, importlib.util.find_spec("yaml12") is None
        """)
    client.send(python=python)
    assert last_tool_text(client) == "(42, True, True)\n"

    python = code("""
        import os; import sys; import yaml12
        (sentinel, os.getpid() == worker_pid, sys.prefix != initial_prefix, yaml12.__name__)
        """)
    client.send(
        python=python,
        requirements={"python": ["py-yaml12"]},
    )
    assert last_tool_text(client) == "(42, True, True, 'yaml12')\n"

    client.send(control="restart")
    assert last_tool_text(client) == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
    )
    client.send(r="is.null(reticulate::py_require()$python_version)")
    assert last_tool_text(client) == "[1] TRUE\n"
    # fmt: python
    python = code("""
        import yaml12

        "sentinel" in globals(), yaml12.__name__
        """)
    client.send(python=python)
    assert last_tool_text(client) == "(False, 'yaml12')\n"
    return client._finish()


def test_failed_live_python_requirements_do_not_run_cell(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.send(python="import os; live_sentinel = 42; live_worker_pid = os.getpid()")
    assert last_tool_text(client) == "[done]"

    # fmt: r
    r = code(r"""
        reticulate_namespace <- asNamespace("reticulate")
        original_py_require <- get("py_require", envir = reticulate_namespace)
        unlockBinding("py_require", reticulate_namespace)
        assign(
          "py_require",
          function(...) stop("synthetic live Python preparation failure"),
          envir = reticulate_namespace
        )
        lockBinding("py_require", reticulate_namespace)
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"

    result = client.send(
        python="failed_live_python_cell = True",
        requirements={"python": ["py-yaml12"]},
    )
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "synthetic live Python preparation failure"
    ), result

    # fmt: r
    r = code(r"""
        unlockBinding("py_require", reticulate_namespace)
        assign(
          "py_require",
          original_py_require,
          envir = reticulate_namespace
        )
        lockBinding("py_require", reticulate_namespace)
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"

    python = code("""
        import os
        import yaml12

        (
            live_sentinel,
            os.getpid() == live_worker_pid,
            "failed_live_python_cell" not in globals(),
            yaml12.__name__,
        )
        """)
    client.send(
        python=python,
        requirements={"python": ["py-yaml12"]},
    )
    assert last_tool_text(client) == "(42, True, True, 'yaml12')\n"
    return client._finish()


def test_prepares_after_idle_python_resolution(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.send(requirements={"r": ["later"]})

    # fmt: r
    r = code(r"""
        callback_gate <- tempfile("mcp-console-callback-gate-")
        callback_checkpoint <- tempfile("mcp-console-callback-checkpoint-")
        run_callback <- function() {
          if (!file.exists(callback_gate)) {
            later::later(run_callback, delay = 0.01)
            return(invisible(NULL))
          }
          stopifnot(file.create(callback_checkpoint))
          reticulate::py_require("py-yaml12")
          reticulate::py_config()
          cat("idle Python ready\n")
        }
        later::later(run_callback, delay = 0.01)
        cat(callback_gate, callback_checkpoint, sep = "\n")
        """)
    client.send(r=r)
    release_worker_callback_gate(client, "idle Python callback")

    client.send(
        requirements={"python": ["py-yaml12"]},
    )
    assert last_tool_text(client) == "[prepared]"
    client.send(r="reticulate::py_require()$packages")
    assert "idle Python ready\n" in last_tool_text(client)
    assert '"py-yaml12"' in last_tool_text(client)
    return client._finish()


def test_retains_idle_python_activation_during_continuous_collection(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.send(requirements={"r": ["later"]})
    client.send(r="invisible(reticulate::py_config())")
    assert last_tool_text(client) == "[done]"

    # fmt: r
    r = code(r"""
        callback_gate <- tempfile("mcp-console-callback-gate-")
        callback_checkpoint <- tempfile("mcp-console-callback-checkpoint-")
        callback_complete <- tempfile("mcp-console-callback-complete-")
        run_callback <- function() {
          if (!file.exists(callback_gate)) {
            later::later(run_callback, delay = 0.01)
            return(invisible(NULL))
          }
          stopifnot(file.create(callback_checkpoint))
          reticulate::py_require("py-yaml12")
          cat("idle Python activated\n")
          stopifnot(file.create(callback_complete))
        }
        later::later(run_callback, delay = 0.01)
        cat(callback_gate, callback_checkpoint, callback_complete, sep = "\n")
        """)
    client.send(r=r)
    (callback_complete,) = release_worker_callback_gate(
        client,
        "idle Python activation",
        ("complete",),
    )
    deadline = time.monotonic() + 30
    while not callback_complete.exists():
        assert client.process.poll() is None, (
            "mcp-console stopped before idle Python activation completed"
        )
        if time.monotonic() >= deadline:
            raise AssertionError("idle Python activation did not complete")
        time.sleep(0.01)
    wait_for_idle_output(
        client,
        "idle Python activated\n\n[idle]",
        "idle Python activation output",
    )

    client.send(control="restart")
    assert last_tool_text(client) == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
    )
    client.send(python="import yaml12; yaml12.__name__")
    assert last_tool_text(client) == "'yaml12'\n"
    return client._finish()


def test_does_not_retain_stale_python_materialization(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.send(r="invisible(reticulate::py_config())")
    assert last_tool_text(client) == "[done]"

    # Make explicit preparation resolve the unchanged environment before its
    # real activation. The first candidate is materialized but never activated.
    # fmt: r
    r = code(r"""
        namespace <- asNamespace("reticulate")
        original_py_require <- get("py_require", envir = namespace)
        injected <- FALSE
        replacement <- function(...) {
          if (!injected) {
            injected <<- TRUE
            requirements <- original_py_require()
            invisible(get("uv_get_or_create_env", envir = namespace)(
              requirements$packages,
              requirements$python_version,
              requirements$exclude_newer
            ))
          }
          original_py_require(...)
        }
        unlockBinding("py_require", namespace)
        assign("py_require", replacement, envir = namespace)
        lockBinding("py_require", namespace)
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"

    client.send(
        requirements={"python": ["py-yaml12"]},
    )
    assert last_tool_text(client) == "[prepared]"
    client.send(control="restart")
    assert last_tool_text(client) == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
    )
    client.send(python="import yaml12; yaml12.__name__")
    assert last_tool_text(client) == "'yaml12'\n"
    return client._finish()


def test_failed_restart_requirements_preserve_worker(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.send(python="restart_marker = 42")
    assert last_tool_text(client) == "[done]"
    invalid = "not a valid requirement !!!"

    client.send(
        control="restart",
        requirements={"python": [invalid]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == named_requirement_error(invalid)

    client.send(python="restart_marker")
    assert last_tool_text(client) == "42\n"
    return client._finish()


def test_layers_python_requirements_declared_by_r_packages(
    binary: Path,
) -> Transcript:
    environment, rscript = r_test_environment()
    fixture = Path(__file__).parents[3] / "fixtures" / "py_require"
    with tempfile.TemporaryDirectory() as library:
        subprocess.run(
            [
                rscript.with_name("R"),
                "CMD",
                "INSTALL",
                f"--library={library}",
                fixture,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        environment["R_LIBS"] = os.pathsep.join(
            filter(None, (library, environment.get("R_LIBS")))
        )
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        # fmt: python
        python = code("""
            import importlib.util
            import sys

            runtime_marker = 42
            initial_prefix = sys.prefix
            importlib.util.find_spec("yaml12") is None
            """)
        client.send(python=python)
        assert last_tool_text(client) == "True\n"

        # fmt: r
        r = code(r"""
            initial_libpython <- reticulate::py_config()$libpython
            initial_worker <- Sys.getpid()
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]"

        client.send(r="library(mcpconsolepyrequire)")
        assert last_tool_text(client) == "[done]"

        # fmt: r
        r = code(r"""
            identical(reticulate::py_config()$libpython, initial_libpython) &&
              identical(Sys.getpid(), initial_worker)
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[1] TRUE\n"

        # fmt: python
        python = code("""
            import yaml12

            (runtime_marker, yaml12.__name__, sys.prefix != initial_prefix)
            """)
        client.send(python=python)
        output = last_tool_text(client)
        assert output == "(42, 'yaml12', True)\n", repr(output)

        client.send(control="restart")
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )

        # fmt: python
        python = code("""
            import yaml12

            ("runtime_marker" in globals(), yaml12.__name__)
            """)
        client.send(python=python)
        assert last_tool_text(client) == "(False, 'yaml12')\n"

        client.send(
            requirements={"python": ["py-yaml12"]},
        )
        assert last_tool_text(client) == "[prepared]"
        return client._finish()


def test_does_not_retain_package_requirements_before_python_initializes(
    binary: Path,
) -> Transcript:
    environment, rscript = r_test_environment()
    fixture = Path(__file__).parents[3] / "fixtures" / "py_require"
    with tempfile.TemporaryDirectory() as library:
        subprocess.run(
            [
                rscript.with_name("R"),
                "CMD",
                "INSTALL",
                f"--library={library}",
                fixture,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        environment["R_LIBS"] = os.pathsep.join(
            filter(None, (library, environment.get("R_LIBS")))
        )
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        # fmt: r
        r = code(r"""
            library(mcpconsolepyrequire)
            request <- tail(reticulate::py_require()$history, 1L)[[1L]]
            stopifnot(
              identical(request$requested_from, "mcpconsolepyrequire"),
              isTRUE(request$env_is_package)
            )
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]"

        # A lazy declaration is worker-owned until Python initializes or an
        # explicit preparation materializes it.
        # fmt: r
        r = code(r"""
            tools::pskill(Sys.getpid(), signal = 9L)
            """).removesuffix("\n")
        client.send(r=r)
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        actual = result["content"][0]["text"]
        assert actual == (
            "[worker sideband read failed: worker sideband closed]\n"
            "[worker terminated by signal 9]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        ), repr(actual)

        # fmt: r
        r = code(r"""
            "py-yaml12" %in% reticulate::py_require()$packages
            """)
        client.send(r=r)
        output = last_tool_text(client)
        assert output == "[1] FALSE\n", repr(output)
        return client._finish()


def test_retains_python_activation_before_later_cell_failure(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        invisible(reticulate::py_config())
        invisible(reticulate::py_require("py-yaml12"))
        stopifnot(reticulate::py_module_available("yaml12"))
        tools::pskill(Sys.getpid(), signal = 9L)
        """).removesuffix("\n")
    client.send(r=r)
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "[worker sideband read failed: worker sideband closed]\n"
        "[worker terminated by signal 9]\n"
        "[worker stopped: in-memory state lost]\n"
        "[starting new worker]\n"
        "[idle]"
    )

    # The successful activation is retained even though the cell later kills
    # the worker before its ordinary completion message.
    # fmt: r
    r = code(r"""
        worker_pid <- Sys.getpid()
        "py-yaml12" %in% reticulate::py_require()$packages
        """)
    client.send(r=r)
    output = last_tool_text(client)
    assert output == "[1] TRUE\n", repr(output)

    # fmt: python
    python = code("""
        import yaml12

        yaml12.__name__
        """)
    client.send(python=python)
    assert last_tool_text(client) == "'yaml12'\n"
    return client._finish()


if __name__ == "__main__":
    run_this_suite(__file__)
