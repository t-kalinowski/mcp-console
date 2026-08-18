#!/usr/bin/env -S uv run --script

import os
import signal
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from _support import (
    McpClient,
    Transcript,
    assert_result_content,
    code,
    normalize_python_resolution_error,
    r_test_environment,
    reference_plots,
    release_worker_callback_gate,
    run_this_suite,
    stop_client,
    wait_for_worker_file,
)

PLATFORMS = {"darwin"}


def test_preserves_configured_python_environment(binary: Path) -> Transcript:
    environment = os.environ.copy()
    environment["RETICULATE_PYTHON"] = "configured-by-user"
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        Sys.getenv("RETICULATE_PYTHON", unset = NA_character_)
        """)
    client.send(r=r)
    assert last_tool_text(client) == '[1] "configured-by-user"\n'
    return client._finish()


def test_preserves_empty_python_environment(binary: Path) -> Transcript:
    environment = os.environ.copy()
    environment["RETICULATE_PYTHON"] = ""
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        Sys.getenv("RETICULATE_PYTHON", unset = NA_character_)
        """)
    client.send(r=r)
    assert last_tool_text(client) == '[1] ""\n'
    return client._finish()


def managed_python_transcript(binary: Path, configured: bool) -> Transcript:
    environment = os.environ.copy()
    if configured:
        environment["RETICULATE_PYTHON"] = "managed"
    else:
        environment.pop("RETICULATE_PYTHON", None)
    uv = shutil.which("uv")
    assert uv is not None, "real uv is required for managed-Python tests"
    environment.pop("RETICULATE_UV", None)
    environment["UV_OFFLINE"] = "1"

    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        python <- Sys.getenv("RETICULATE_PYTHON", unset = NA_character_)
        config <- reticulate::py_config()
        history <- reticulate::py_require()$history
        stopifnot(
          identical(python, "managed"),
          file.exists(config$python),
          isTRUE(config$ephemeral),
          "pandas" %in% reticulate::py_require()$packages,
          !any(vapply(
            history,
            function(request) identical(request$requested_from, "base"),
            logical(1L)
          ))
        )
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]", client.transcript[-1]
    # fmt: python
    python = code("""
        import io
        import pandas as pd

        frame = pd.read_csv(io.StringIO("value\\n40\\n2\\n"))
        int(frame["value"].sum())
        """)
    client.send(python=python)
    output = last_tool_text(client)
    assert output == "42\n", repr(output)
    return client._finish()


def test_evaluates_with_default_managed_python(binary: Path) -> Transcript:
    return managed_python_transcript(binary, configured=False)


def test_evaluates_with_explicit_managed_python(binary: Path) -> Transcript:
    return managed_python_transcript(binary, configured=True)


def test_uses_200_column_default(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: python
    python = code("""
        import shutil

        import numpy as np
        import pandas as pd

        print(f"terminal columns: {shutil.get_terminal_size().columns}")
        print(f"pandas display.width: {pd.get_option('display.width')}")
        print(f"NumPy linewidth: {np.get_printoptions()['linewidth']}")
        pd.DataFrame(
            [range(12)],
            columns=[f"column_{column:02}" for column in range(12)],
        )
        """)
    client.send(python=python)
    output = last_tool_text(client)
    assert output.startswith(
        "terminal columns: 200\npandas display.width: 200\nNumPy linewidth: 200\n"
    ), repr(output)
    for column in range(12):
        assert f"column_{column:02}" in output
    assert "..." not in output
    assert "[1 rows x 12 columns]" not in output
    return client._finish()


def test_uses_200_column_default_after_r_initializes_python(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        reticulate::py_run_string(
          "
        import numpy as np
        import pandas as pd
        "
        )
        cat(
          "R-first NumPy linewidth: ",
          reticulate::py_eval("np.get_printoptions()['linewidth']"),
          "\nR-first pandas display.width: ",
          reticulate::py_eval("pd.get_option('display.width')"),
          "\n",
          sep = ""
        )
        """)
    client.send(r=r)
    assert last_tool_text(client) == (
        "R-first NumPy linewidth: 200\nR-first pandas display.width: 200\n"
    )
    return client._finish()


def test_prints_requirements_with_host_uv_cache(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        environment = os.environ.copy()
        uv_cache = str(temporary / "uv-cache")
        environment["UV_CACHE_DIR"] = str(temporary / "startup-uv-cache")
        environment["MCP_CONSOLE_TEST_UV_CACHE_DIR"] = uv_cache
        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=temporary,
        )
        client._initialize_and_list_tools()
        # fmt: r
        r = code(r"""
            cache <- Sys.getenv("MCP_CONSOLE_TEST_UV_CACHE_DIR")
            stopifnot(!file.exists(cache))
            Sys.setenv(UV_CACHE_DIR = cache)
            printed <- capture.output(print(reticulate::py_require()))
            stopifnot(
              length(printed) > 0L,
              dir.exists(cache),
              identical(Sys.getenv("UV_CACHE_DIR"), cache)
            )
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]", client.transcript[-1]
        return client._finish()


def test_recovers_from_python_version_resolution_failure(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        worker_pid <- Sys.getpid()
        base::local({
          old_cache <- Sys.getenv("UV_CACHE_DIR", unset = NA_character_)
          on.exit(
            if (is.na(old_cache)) {
              Sys.unsetenv("UV_CACHE_DIR")
            } else {
              Sys.setenv(UV_CACHE_DIR = old_cache)
            }
          )
          Sys.setenv(UV_CACHE_DIR = "/dev/null")
          print(reticulate::py_require())
        })
        """)
    client.send(r=r)
    result = client.transcript[-1]["result"]
    assert result["isError"] is False, result
    output = result["content"][0]["text"]
    assert "managed Python version resolution failed" in output
    uv = shutil.which("uv")
    assert uv is not None and output.count(uv) == 1, output
    result["content"][0]["text"] = output.replace(uv, "<uv executable>")

    client.send(r="identical(Sys.getpid(), worker_pid)")
    assert last_tool_text(client) == "[1] TRUE\n"
    return client._finish()


def test_prepares_initial_python_requirements(binary: Path) -> Transcript:
    environment = os.environ.copy()
    environment["RETICULATE_PYTHON"] = "/mcp-console-prepare-must-replace-python"
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    client.session(
        action="prepare",
        requirements={"python": ["py-yaml12"]},
    )
    assert last_tool_text(client) == "[prepared]"
    invalid = "not a valid requirement !!!"

    client.session(
        action="prepare",
        requirements={"python": [invalid]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    resolution_error = result["content"][0]["text"]
    recorded_error = normalize_python_resolution_error(resolution_error, invalid)
    result["content"][0]["text"] = recorded_error
    client.session(
        action="prepare",
        requirements={"python": [invalid]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    result["content"][0]["text"] = normalize_python_resolution_error(
        result["content"][0]["text"], invalid
    )
    assert result["content"][0]["text"] == recorded_error
    client.session(
        action="prepare",
        requirements={"python": ["numpy\npandas"]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "Python requirement strings must not contain NUL or line breaks"
    )
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
    client.session(
        action="prepare",
        requirements={"python": ["py-yaml12"]},
    )
    assert last_tool_text(client) == "[prepared]"
    return client._finish()


def test_prepares_explicit_numpy_requirement(binary: Path) -> Transcript:
    configured = "/mcp-console-prepare-must-replace-python"
    environment = os.environ.copy()
    environment["RETICULATE_PYTHON"] = configured
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    client.session(
        action="prepare",
        requirements={"python": ["numpy"]},
    )
    assert last_tool_text(client) == "[prepared]"
    # fmt: r
    r = code(rf"""
        stopifnot(Sys.getenv("RETICULATE_PYTHON") != "{configured}")
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"
    return client._finish()


def test_does_not_fail_resolution_when_matplotlib_cache_cannot_be_written(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        environment = os.environ.copy()
        cache_directory = temporary / "user-matplotlib"
        environment["MPLCONFIGDIR"] = str(cache_directory)
        environment["XDG_CACHE_HOME"] = str(temporary / "host-cache")
        environment["MPL_IGNORE_SYSTEM_FONTS"] = "1"
        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=temporary,
        )
        client._initialize_and_list_tools()
        client.session(
            action="prepare",
            requirements={"python": ["matplotlib"]},
        )
        assert last_tool_text(client) == "[prepared]"
        caches = list(cache_directory.glob("fontlist-v*.json"))
        assert len(caches) == 1, caches
        caches[0].unlink()
        caches[0].mkdir()

        client.session(
            action="prepare",
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
    client.session(
        action="prepare",
        requirements={"python": ["py-yaml12"]},
    )
    assert last_tool_text(client) == "[prepared]"
    client.send(python="restart_marker = 42")
    assert last_tool_text(client) == "[done]"

    client.session(action="restart")
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
    client.session(
        action="prepare",
        requirements={"python": [invalid]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    assert "managed Python resolution failed" in result["content"][0]["text"]
    result["content"][0]["text"] = normalize_python_resolution_error(
        result["content"][0]["text"], invalid
    )

    python = code("""
        sentinel, os.getpid() == worker_pid, importlib.util.find_spec("yaml12") is None
        """)
    client.send(python=python)
    assert last_tool_text(client) == "(42, True, True)\n"

    client.session(
        action="prepare",
        requirements={"python": ["py-yaml12"]},
    )
    assert last_tool_text(client) == "[prepared]"

    python = code("""
        import os; import sys; import yaml12
        (sentinel, os.getpid() == worker_pid, sys.prefix != initial_prefix, yaml12.__name__)
        """)
    client.send(python=python)
    assert last_tool_text(client) == "(42, True, True, 'yaml12')\n"

    client.session(action="restart")
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


def test_prepares_after_idle_python_resolution(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.session(action="prepare", requirements={"r": ["later"]})

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

    client.session(
        action="prepare",
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
    client.session(action="prepare", requirements={"r": ["later"]})
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
    client.send()
    output = last_tool_text(client)
    assert output == "idle Python activated\n\n[idle]", repr(output)

    client.session(action="restart")
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

    client.session(
        action="prepare",
        requirements={"python": ["py-yaml12"]},
    )
    assert last_tool_text(client) == "[prepared]"
    client.session(action="restart")
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

    client.session(
        action="restart",
        requirements={"python": [invalid]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    result["content"][0]["text"] = normalize_python_resolution_error(
        result["content"][0]["text"], invalid
    )

    client.send(python="restart_marker")
    assert last_tool_text(client) == "42\n"
    return client._finish()


def test_layers_python_requirements_declared_by_r_packages(
    binary: Path,
) -> Transcript:
    environment, rscript = r_test_environment()
    fixture = Path(__file__).parents[1] / "fixtures" / "py_require"
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

        client.session(action="restart")
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

        client.session(
            action="prepare",
            requirements={"python": ["py-yaml12"]},
        )
        assert last_tool_text(client) == "[prepared]"
        return client._finish()


def test_does_not_retain_package_requirements_before_python_initializes(
    binary: Path,
) -> Transcript:
    environment, rscript = r_test_environment()
    fixture = Path(__file__).parents[1] / "fixtures" / "py_require"
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


def test_rejects_python_preparation_while_evaluation_is_running(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        python = code("""
            runtime_generation_marker = "original runtime retained"
            import time; from pathlib import Path
            temporary = Path(__import__("os").environ["TMPDIR"])
            (temporary / "python-evaluation-running").touch()
            while not (temporary / "release-python").exists():
                time.sleep(0.01)
            """)
        client.send(python=python, timeout_ms=0)
        assert last_tool_text(client) == "\n[running]"
        client.transcript[-1]["result"]["content"][0]["text"] = "<running>"
        running = wait_for_worker_file(
            Path(temporary_directory),
            "python-evaluation-running",
            client,
        )
        release = running.parent / "release-python"

        session_returned = threading.Event()
        forced_release = threading.Event()

        def release_blocked_evaluation() -> None:
            if not session_returned.wait(2):
                forced_release.set()
                release.touch()

        watchdog = threading.Thread(target=release_blocked_evaluation)
        watchdog.start()
        client.session(
            action="prepare",
            requirements={"python": ["py-yaml12"]},
        )
        session_returned.set()
        watchdog.join()
        assert not forced_release.is_set(), "session waited for the running evaluation"
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        assert result["content"][0]["text"] == (
            "worker is already evaluating a cell; poll it before preparing requirements"
        )

        release.touch()
        client.send()
        assert last_tool_text(client) == "[done]"
        client.send(python="runtime_generation_marker")
        assert last_tool_text(client) == "'original runtime retained'\n"
        return client._finish()


def test_interrupts_running_python_evaluation(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(binary, ("serve",), environment)
        passed = False
        try:
            client._initialize_and_list_tools()
            # fmt: r
            r = code(r"""
                invisible(suppressMessages(base::trace(
                  "py_run_string",
                  tracer = quote({
                    invisible(file.create(file.path(
                      tempdir(),
                      "python-r-interrupt-started"
                    )))
                    repeat {}
                  }),
                  print = FALSE,
                  where = asNamespace("reticulate")
                )))
                """)
            client.send(r=r)
            output = last_tool_text(client)
            assert output == "[done]", repr(output)

            client.send(python="42", timeout_ms=0)
            assert last_tool_text(client) == "\n[running]"
            wait_for_worker_file(
                temporary_path,
                "python-r-interrupt-started",
                client,
            )

            client.session(action="interrupt")
            assert last_tool_text(client) == "[interrupt sent]"
            client.send(timeout_ms=3_000)
            result = client.transcript[-1]["result"]
            assert result["isError"] is False, result
            output = last_tool_text(client)
            assert output == "\n", repr(output)

            # fmt: r
            r = code(r"""
                invisible(suppressMessages(base::untrace(
                  "py_run_string",
                  where = asNamespace("reticulate")
                )))
                """)
            client.send(r=r)
            assert last_tool_text(client) == "[done]"

            # fmt: python
            python = code("""
                import os
                from pathlib import Path

                python_interrupt_state = 41
                Path(
                    os.environ["TMPDIR"],
                    "python-interrupt-started",
                ).touch()
                while True:
                    pass
                """)
            client.send(python=python, timeout_ms=0)
            assert last_tool_text(client) == "\n[running]"
            wait_for_worker_file(
                temporary_path,
                "python-interrupt-started",
                client,
            )

            client.session(action="interrupt")
            assert last_tool_text(client) == "[interrupt sent]"
            client.send(timeout_ms=3_000)
            assert "KeyboardInterrupt" in last_tool_text(client)

            client.send(python="python_interrupt_state + 1")
            assert last_tool_text(client) == "42\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_client(client)


def test_interrupts_live_python_resolver(binary: Path) -> Transcript:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    connected = threading.Event()
    resolver_stopped = threading.Event()
    release = threading.Event()

    def hold_index_connection() -> None:
        connection, _ = listener.accept()
        listener.close()
        connected.set()
        connection.settimeout(0.05)
        with connection:
            while not release.is_set():
                try:
                    if not connection.recv(4096):
                        resolver_stopped.set()
                        return
                except TimeoutError:
                    pass

    index = threading.Thread(target=hold_index_connection, daemon=True)
    index.start()

    environment = os.environ.copy()
    environment["RUST_LOG"] = "error"
    previous_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    try:
        client = McpClient(binary, ("serve",), environment)
    finally:
        signal.signal(signal.SIGINT, previous_handler)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    passed = False
    try:
        client._initialize_and_list_tools()
        # fmt: r
        r = code(rf"""
            resolver_interrupt_state <- 41L
            Sys.setenv(UV_DEFAULT_INDEX = "http://127.0.0.1:{port}/simple")
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]"

        preparation = client._start_session(
            action="prepare",
            requirements={"python": ["mcp-console-blocked-live-preparation"]},
        )
        assert connected.wait(10), "live preparation did not contact the blocking index"

        interrupt = client._start_session(action="interrupt")
        preparation_returned = threading.Event()
        forced_release = threading.Event()

        def release_if_preparation_blocks() -> None:
            if not preparation_returned.wait(2):
                forced_release.set()
                release.set()

        watchdog = threading.Thread(target=release_if_preparation_blocks)
        watchdog.start()
        client._receive_many([preparation, interrupt])
        preparation_returned.set()
        watchdog.join()
        assert not forced_release.is_set(), "interrupt did not stop the Python resolver"
        assert interrupt["result"] == {
            "content": [{"type": "text", "text": "[interrupt sent]"}],
            "isError": False,
        }, interrupt
        assert resolver_stopped.wait(2), "interrupted resolver kept its connection open"
        assert preparation["result"]["isError"] is True, preparation
        error = preparation["result"]["content"][0]["text"]
        assert "managed Python resolution" in error, error
        error = normalize_python_resolution_error(error)
        assert "uv output:" in error, error
        preparation["result"]["content"][0]["text"] = error.replace(
            f"127.0.0.1:{port}", "127.0.0.1:<PORT>"
        )

        client.send(r="resolver_interrupt_state + 1L")
        assert last_tool_text(client) == "[1] 42\n"
        transcript = client._finish()
        address = f"127.0.0.1:{port}"
        for entry in transcript:
            if source := entry.get("send", {}).get("r"):
                entry["send"]["r"] = source.replace(address, "127.0.0.1:<PORT>")
        passed = True
        return transcript
    finally:
        release.set()
        index.join(2)
        if not passed:
            stop_client(client)


def test_restart_cancels_live_python_preparation(binary: Path) -> Transcript:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    connected = threading.Event()
    resolver_stopped = threading.Event()
    release = threading.Event()

    def hold_index_connection() -> None:
        connection, _ = listener.accept()
        listener.close()
        connected.set()
        connection.settimeout(0.05)
        with connection:
            while not release.is_set():
                try:
                    if not connection.recv(4096):
                        resolver_stopped.set()
                        return
                except TimeoutError:
                    pass

    index = threading.Thread(target=hold_index_connection, daemon=True)
    index.start()

    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: r
    r = code(rf"""
        restart_marker <- 42L
        Sys.setenv(UV_DEFAULT_INDEX = "http://127.0.0.1:{port}/simple")
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"

    preparation = client._start_session(
        action="prepare",
        requirements={"python": ["mcp-console-blocked-live-preparation"]},
    )
    assert connected.wait(10), "live preparation did not contact the blocking index"

    calls_returned = threading.Event()
    forced_release = threading.Event()

    def release_if_calls_block() -> None:
        if not calls_returned.wait(2):
            forced_release.set()
            release.set()

    watchdog = threading.Thread(target=release_if_calls_block)
    watchdog.start()
    poll = client._start_send()
    second_prepare = client._start_session(
        action="prepare",
        requirements={"python": ["py-yaml12"]},
    )
    client._receive_many([poll, second_prepare])
    calls_returned.set()
    watchdog.join()
    assert not forced_release.is_set(), "another tool call waited for live preparation"
    assert poll["result"] == {
        "content": [{"type": "text", "text": "[session is preparing requirements]"}],
        "isError": True,
    }, poll
    assert second_prepare["result"] == {
        "content": [{"type": "text", "text": "session is preparing requirements"}],
        "isError": True,
    }, second_prepare

    restart = client._start_session(action="restart")
    client._receive_many([preparation, restart])

    preparation_result = preparation["result"]
    assert preparation_result == {
        "content": [
            {
                "type": "text",
                "text": "Python preparation cancelled by restart",
            }
        ],
        "isError": True,
    }, preparation_result
    assert restart["result"]["content"] == [
        {
            "type": "text",
            "text": (
                "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
            ),
        }
    ], restart
    assert resolver_stopped.wait(2), "restart did not stop the Python resolver"
    release.set()
    index.join(2)

    # fmt: r
    r = code(r"""
        requirements <- reticulate::py_require()
        stopifnot(
          !exists("restart_marker", inherits = FALSE),
          !"mcp-console-blocked-live-preparation" %in% requirements$packages
        )
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"
    transcript = client._finish()
    address = f"127.0.0.1:{port}"
    for entry in transcript:
        if source := entry.get("send", {}).get("r"):
            entry["send"]["r"] = source.replace(address, "127.0.0.1:<PORT>")
    return transcript


def test_does_not_parse_requirements_as_rscript_options(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        marker = Path(temporary_directory) / "host-r-code-ran"
        expression = (
            "base::writeLines('executed', base::Sys.getenv('MCP_CONSOLE_HOST_MARKER'))"
        )
        environment = os.environ.copy()
        environment["RETICULATE_PYTHON"] = "/mcp-console-prepare-must-replace-python"
        environment["MCP_CONSOLE_HOST_MARKER"] = str(marker)
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        client.session(
            action="prepare",
            requirements={"python": ["-e", expression]},
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        assert not marker.exists(), "requirement executed as unsandboxed R code"
        assert "managed Python resolution failed" in result["content"][0]["text"]
        result["content"][0]["text"] = normalize_python_resolution_error(
            result["content"][0]["text"]
        )
        return client._finish()


def test_forces_uv_offline_in_builtin_worker(binary: Path) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    environment["UV_OFFLINE"] = "0"
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        Sys.getenv("UV_OFFLINE", unset = NA_character_)
        """)
    client.send(r=r)
    assert last_tool_text(client) == '[1] "1"\n'
    return client._finish()


def test_evaluates_cells_in_persistent_reticulate_state(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        from_r <- 40L
        python_source_visible <- function() {
          calls <- vapply(sys.calls(), deparse1, character(1))
          marker <- paste0("unique_python_", "source_marker")
          any(grepl(marker, calls, fixed = TRUE))
        }
        """)
    client.send(r=r)
    # fmt: python
    python = code("""
        answer = r.from_r + 1
        print("from Python")
        answer + 1
        """)
    client.send(python=python)
    output = last_tool_text(client)
    assert output == "from Python\n42\n", repr(output)
    # fmt: python
    python = code("""
        1
        2
        """)
    client.send(python=python)
    assert last_tool_text(client) == "2\n"
    client.send(python="answer")
    assert last_tool_text(client) == "41\n"
    # fmt: r
    r = code(r"""
        stopifnot(!"package:reticulate" %in% search())
        py <- "user shadow"
        stopifnot(identical(py, "user shadow"))
        rm(py)
        py$answer
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[1] 41\n"
    # fmt: python
    python = code("""
        unique_python_source_marker = r.python_source_visible()
        unique_python_source_marker
        """)
    client.send(python=python)
    output = last_tool_text(client)
    assert output == "False\n", repr(output)
    # fmt: r
    r = code(r"""
        .mcp_console_private <- "user value"
        .mcp_console_python_source <- "user source"
        .mcp_console_python_filename <- "user filename"
        is.null <- function(...) FALSE
        """)
    client.send(r=r)
    client.send(python="answer + 1")
    assert last_tool_text(client) == "42\n"
    # fmt: python
    python = code("""
        compile = "user compile"
        eval = "user eval"
        exec = "user exec"
        isinstance = "user isinstance"
        BaseException = "user BaseException"
        """)
    client.send(python=python)
    assert last_tool_text(client) == "[done]"
    client.send(python="answer + 1")
    assert last_tool_text(client) == "42\n"
    client.send(python="silent = True")
    assert last_tool_text(client) == "[done]"
    # fmt: r
    r = code(r"""
        rm(list = ls())
        py$assigned_from_r <- 43L
        py$answer
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[1] 41\n"
    # fmt: python
    python = code("""
        assigned_from_r
        """)
    client.send(python=python)
    assert last_tool_text(client) == "43\n"
    return client._finish()


def test_returns_r_plots_from_python_bridge(binary: Path) -> Transcript:
    environment, rscript = r_test_environment()
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        bridge_plot <- function() {
          plot(1:3)
          invisible(NULL)
        }
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"

    expected_plot = reference_plots(
        rscript,
        environment,
        r + "bridge_plot()\n",
        width=800 / 96,
        height=600 / 96,
        dpi=96,
        pages=1,
    )
    # fmt: python
    python = code("""
        print("before plot")
        r.bridge_plot()
        print("after plot")
        """)
    client.send(python=python)
    assert_result_content(
        client,
        ["before plot\nafter plot\n", expected_plot[0]],
    )
    return client._finish()


def test_returns_matplotlib_plots(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        workspace = temporary / "workspace-one"
        workspace.mkdir()
        system_profiler = shutil.which("system_profiler")
        assert system_profiler is not None, "system_profiler is required"
        path = os.environ.get("PATH")
        assert path is not None, "PATH is required"
        probe = temporary / "bin" / "system_profiler"
        probe.parent.mkdir()
        probe.write_text(
            code(r"""
                #!/bin/sh
                : > "$TMPDIR/mcp-console-font-discovery"
                exec "$MCP_CONSOLE_TEST_SYSTEM_PROFILER" "$@"
                """),
            encoding="utf-8",
        )
        probe.chmod(0o755)
        fontconfig = temporary / "fonts.conf"
        fontconfig.write_text(
            code(r"""
                <?xml version="1.0"?>
                <!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
                <fontconfig>
                  <cachedir prefix="xdg">mcp-console-test</cachedir>
                </fontconfig>
                """),
            encoding="utf-8",
        )
        host_matplotlib = temporary / "host-matplotlib"
        host_matplotlib.mkdir()
        host_matplotlibrc = host_matplotlib / "matplotlibrc"
        host_matplotlibrc.write_text("lines.linewidth: 7.25\n", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["FONTCONFIG_FILE"] = str(fontconfig)
        environment["MPLCONFIGDIR"] = str(host_matplotlib)
        environment["XDG_CACHE_HOME"] = str(temporary / "host-cache")
        environment["MCP_CONSOLE_TEST_MATPLOTLIBRC"] = str(host_matplotlibrc)
        environment["MCP_CONSOLE_TEST_SYSTEM_PROFILER"] = system_profiler
        environment["PATH"] = os.pathsep.join((str(probe.parent), path))
        environment.pop("MATPLOTLIBRC", None)
        environment.pop("MPL_IGNORE_SYSTEM_FONTS", None)
        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=workspace,
        )
        client._initialize_and_list_tools()
        # fmt: r
        r = code(r"""
            reticulate::py_require("matplotlib")
            invisible(reticulate::py_config())
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]"
        host_discovery = temporary / "mcp-console-font-discovery"
        assert host_discovery.is_file()
        persistent_caches = list(host_matplotlib.glob("fontlist-v*.json"))
        assert len(persistent_caches) == 1, persistent_caches
        persistent_cache_bytes = persistent_caches[0].read_bytes()
        host_discovery.unlink()

        # fmt: python
        python = code("""
            import os
            from pathlib import Path

            import matplotlib
            import matplotlib.pyplot as plt

            assert (
                Path(matplotlib.matplotlib_fname()).resolve()
                == Path(os.environ["MCP_CONSOLE_TEST_MATPLOTLIBRC"]).resolve()
            )
            assert matplotlib.rcParams["lines.linewidth"] == 7.25

            later_figure, later_axes = plt.subplots(num=20)
            later_axes.plot([1, 2, 3], [1, 2, 1])
            later_reference = Path(os.environ["TMPDIR"]) / "matplotlib-later-reference.png"
            later_figure.savefig(later_reference, format="png")

            figure, axes = plt.subplots(num=10)
            axes.plot([1, 2, 3], [3, 1, 2])
            invalid_cache = Path(os.environ["MPLCONFIGDIR"]) / "fontlist-v999.json"
            invalid_cache.write_text(
                '{"__class__":"FontManager","_version":999}',
                encoding="utf-8",
            )

            reference = Path(os.environ["TMPDIR"]) / "matplotlib-reference.png"
            figure.savefig(reference, format="png")
            """)
        client.send(python=python)
        assert not list(temporary.rglob("mcp-console-font-discovery"))
        reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-reference.png",
            client,
        )
        later_reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-later-reference.png",
            client,
        )
        assert_result_content(
            client,
            [reference.read_bytes(), later_reference.read_bytes()],
            image_reference="live matplotlib savefig {page}",
        )

        # fmt: python
        python = code("""
            shown_figure, shown_axes = plt.subplots()
            shown_axes.plot([1, 2, 3], [1, 3, 2])
            shown_reference = Path(os.environ["TMPDIR"]) / "matplotlib-shown-reference.png"
            shown_figure.savefig(shown_reference, format="png")
            print("before show")
            plt.show()
            print("after show")
            shown_figure
            """)
        client.send(python=python)
        shown_reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-shown-reference.png",
            client,
        )
        result = client.transcript[-1]["result"]
        output = result["content"][0]["text"]
        assert output.startswith("before show\nafter show\n<Figure size "), output
        assert output.endswith(" with 1 Axes>\n"), output
        result["content"][0]["text"] = (
            "before show\nafter show\n<matplotlib figure displayhook representation>\n"
        )
        assert_result_content(
            client,
            [result["content"][0]["text"], shown_reference.read_bytes()],
            image_reference="live shown matplotlib savefig {page}",
        )

        # fmt: python
        python = code("""
            closed_figure, closed_axes = plt.subplots()
            closed_axes.plot([1, 2, 3], [2, 1, 3])
            closed_reference = Path(os.environ["TMPDIR"]) / "matplotlib-closed-reference.png"
            closed_figure.savefig(closed_reference, format="png")
            plt.close(closed_figure)
            plt.get_fignums()
            """)
        client.send(python=python)
        closed_reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-closed-reference.png",
            client,
        )
        assert closed_reference.is_file()
        assert last_tool_text(client) == "[]\n"

        # fmt: python
        python = code("""
            axes.plot([1, 3], [2, 0])
            plt.get_fignums()
            """)
        client.send(python=python)
        assert last_tool_text(client) == "[]\n"

        # fmt: python
        python = code("""
            error_figure, error_axes = plt.subplots()
            error_axes.plot([1, 2], [2, 1])
            error_reference = Path(os.environ["TMPDIR"]) / "matplotlib-error-reference.png"
            error_figure.savefig(error_reference, format="png")
            raise ValueError("cell failed")
            """)
        client.send(python=python)
        result = client.transcript[-1]["result"]
        assert result["isError"] is False, result
        output = result["content"][0]["text"]
        assert output.startswith("Traceback (most recent call last):\n"), output
        assert output.endswith("ValueError: cell failed\n"), output
        error_reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-error-reference.png",
            client,
        )
        assert_result_content(
            client,
            [result["content"][0]["text"], error_reference.read_bytes()],
            image_reference="live error-cell matplotlib savefig {page}",
        )

        client.send(python="plt.get_fignums()")
        assert last_tool_text(client) == "[]\n"

        # fmt: python
        python = code("""
            def fail_plot_capture(*args, **kwargs):
                raise RuntimeError("plot render failed")


            failed_figure = plt.figure()
            failed_figure.savefig = fail_plot_capture
            figure, axes = plt.subplots()
            axes.plot([1, 3], [2, 0])
            second_reference = Path(os.environ["TMPDIR"]) / "matplotlib-second-reference.png"
            figure.savefig(second_reference, format="png")
            """)
        client.send(python=python)
        result = client.transcript[-1]["result"]
        assert result["isError"] is False, result
        output = result["content"][0]["text"]
        assert output.startswith("Traceback (most recent call last):\n"), output
        assert output.endswith("RuntimeError: plot render failed\n"), output
        second_reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-second-reference.png",
            client,
        )
        assert_result_content(
            client,
            [result["content"][0]["text"], second_reference.read_bytes()],
            image_reference="live second matplotlib savefig {page}",
        )

        client.send(python="plt.get_fignums()")
        assert last_tool_text(client) == "[]\n"

        # Replacing the private link must not make a later runtime resolution
        # overwrite user-owned worker state or discard the worker.
        # fmt: python
        python = code("""
            private_cache = next(
                path
                for path in Path(os.environ["MPLCONFIGDIR"]).glob("fontlist-v*.json")
                if path.is_symlink()
            )
            private_cache_bytes = private_cache.read_bytes()
            private_cache.unlink()
            private_cache.write_bytes(private_cache_bytes)
            cache_link_replaced = True
            """)
        client.send(python=python)
        assert last_tool_text(client) == "[done]"

        # fmt: r
        r = code(r"""
            reticulate::py_require("py-yaml12")
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]"
        client.send(python="(cache_link_replaced, __import__('yaml12').__name__)")
        assert last_tool_text(client) == "(True, 'yaml12')\n"

        client.session(action="restart")
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )
        # fmt: python
        python = code("""
            import os
            from pathlib import Path

            marker = Path(os.environ["TMPDIR"]) / "mcp-console-font-discovery"
            invalid_cache = Path(os.environ["MPLCONFIGDIR"]) / "fontlist-v999.json"
            invalid_cache_was_seeded = invalid_cache.exists()

            import matplotlib
            import matplotlib.font_manager

            config = Path(matplotlib.matplotlib_fname())
            font_cache = next(Path(os.environ["MPLCONFIGDIR"]).glob("fontlist-v*.json"))
            try:
                with font_cache.open("a", encoding="utf-8"):
                    pass
            except PermissionError:
                font_cache_read_only = True
            else:
                font_cache_read_only = False

            try:
                with config.open("a", encoding="utf-8"):
                    pass
            except PermissionError:
                config_read_only = True
            else:
                config_read_only = False

            try:
                config.with_name("worker-payload").write_text("payload", encoding="utf-8")
            except PermissionError:
                config_directory_read_only = True
            else:
                config_directory_read_only = False

            private_probe = Path(os.environ["MPLCONFIGDIR"]) / "config-write-probe"
            private_probe.write_text("ok", encoding="utf-8")

            (
                config.resolve() == Path(os.environ["MCP_CONSOLE_TEST_MATPLOTLIBRC"]).resolve(),
                matplotlib.rcParams["lines.linewidth"],
                font_cache_read_only,
                config_read_only,
                config_directory_read_only,
                private_probe.read_text(encoding="utf-8") == "ok",
                marker.exists(),
                invalid_cache_was_seeded,
            )
            """)
        client.send(python=python)
        output = last_tool_text(client)
        assert output == "(True, 7.25, True, True, True, True, False, False)\n", repr(
            output
        )
        assert not list(temporary.rglob("mcp-console-font-discovery"))
        transcript = client._finish()
        assert (
            host_matplotlibrc.read_text(encoding="utf-8") == "lines.linewidth: 7.25\n"
        )
        assert not (host_matplotlib / "worker-payload").exists()
        assert len(persistent_caches) == 1, persistent_caches
        assert persistent_caches[0].read_bytes() == persistent_cache_bytes
        assert not (persistent_caches[0].parent / "fontlist-v999.json").exists()
        assert not list(
            (temporary / "host-cache" / "mcp-console" / "matplotlib").glob(
                "fontlist-v*.json"
            )
        )
        return transcript


def test_inherits_explicit_matplotlib_config(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        explicit = temporary / "explicit"
        explicit.mkdir()
        explicit_rc = explicit / "matplotlibrc"
        explicit_rc.write_text("lines.linewidth: 8.25\n", encoding="utf-8")
        inherited = temporary / "inherited"
        inherited.mkdir()
        (inherited / "matplotlibrc").write_text(
            "lines.linewidth: 18.25\n",
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["XDG_CACHE_HOME"] = str(temporary / "host-cache")
        environment["MPLCONFIGDIR"] = str(inherited)
        environment["MATPLOTLIBRC"] = str(explicit_rc)
        environment["MPL_IGNORE_SYSTEM_FONTS"] = "1"
        environment["MCP_CONSOLE_TEST_MATPLOTLIBRC"] = str(explicit_rc)
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        client.session(
            action="prepare",
            requirements={"python": ["matplotlib"]},
        )
        assert last_tool_text(client) == "[prepared]"
        # fmt: python
        python = code("""
            import os
            from pathlib import Path

            import matplotlib

            config = Path(matplotlib.matplotlib_fname())
            try:
                with config.open("a", encoding="utf-8"):
                    pass
            except PermissionError:
                config_read_only = True
            else:
                config_read_only = False

            private_probe = Path(os.environ["MPLCONFIGDIR"]) / "config-write-probe"
            private_probe.write_text("ok", encoding="utf-8")

            (
                config.resolve() == Path(os.environ["MCP_CONSOLE_TEST_MATPLOTLIBRC"]).resolve(),
                matplotlib.rcParams["lines.linewidth"],
                config_read_only,
                private_probe.read_text(encoding="utf-8") == "ok",
            )
            """)
        client.send(python=python)
        output = last_tool_text(client)
        assert output == "(True, 8.25, True, True)\n", repr(output)
        transcript = client._finish()
        assert explicit_rc.read_text(encoding="utf-8") == "lines.linewidth: 8.25\n"
        assert not list(explicit.glob("fontlist-v*.json"))
        caches = list(inherited.glob("fontlist-v*.json"))
        assert len(caches) == 1, caches
        assert not list(
            (temporary / "host-cache" / "mcp-console" / "matplotlib").glob(
                "fontlist-v*.json"
            )
        )
        return transcript


def test_inherits_default_matplotlib_config(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        home = temporary / "home"
        matplotlib = home / ".matplotlib"
        matplotlib.mkdir(parents=True)
        matplotlibrc = matplotlib / "matplotlibrc"
        matplotlibrc.write_text("lines.linewidth: 9.25\n", encoding="utf-8")
        r_environment, rscript = r_test_environment()
        # fmt: r
        source = code(r"""
            writeLines(.libPaths())
            """)
        r_libraries = subprocess.run(
            [rscript, "--vanilla", "-e", source],
            check=True,
            capture_output=True,
            text=True,
            env=r_environment,
        ).stdout.splitlines()
        uv = shutil.which("uv")
        assert uv is not None, "real uv is required for managed-Python tests"
        uv_cache = subprocess.run(
            [uv, "cache", "dir"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        uv_python = subprocess.run(
            [uv, "python", "dir"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        environment = os.environ.copy()
        environment["HOME"] = str(home)
        environment["TMPDIR"] = temporary_directory
        environment["XDG_CACHE_HOME"] = str(temporary / "host-cache")
        environment["R_LIBS_USER"] = os.pathsep.join(r_libraries)
        environment["RETICULATE_UV"] = uv
        environment["UV_CACHE_DIR"] = uv_cache
        environment["UV_PYTHON_INSTALL_DIR"] = uv_python
        environment["MPL_IGNORE_SYSTEM_FONTS"] = "1"
        environment["MCP_CONSOLE_TEST_MATPLOTLIBRC"] = str(matplotlibrc)
        environment.pop("MATPLOTLIBRC", None)
        environment.pop("MPLCONFIGDIR", None)
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        client.session(
            action="prepare",
            requirements={"python": ["matplotlib"]},
        )
        assert last_tool_text(client) == "[prepared]"
        # fmt: python
        python = code("""
            import os
            from pathlib import Path

            import matplotlib

            (
                Path(matplotlib.matplotlib_fname()).resolve()
                == Path(os.environ["MCP_CONSOLE_TEST_MATPLOTLIBRC"]).resolve(),
                matplotlib.rcParams["lines.linewidth"],
            )
            """)
        client.send(python=python)
        output = last_tool_text(client)
        assert output == "(True, 9.25)\n", repr(output)
        transcript = client._finish()
        assert matplotlibrc.read_text(encoding="utf-8") == "lines.linewidth: 9.25\n"
        caches = list(matplotlib.glob("fontlist-v*.json"))
        assert len(caches) == 1, caches
        assert not list(
            (temporary / "host-cache" / "mcp-console" / "matplotlib").glob(
                "fontlist-v*.json"
            )
        )
        return transcript


def test_runs_async_python_explicitly(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: python
    python = code("""
        import asyncio


        async def answer():
            await asyncio.sleep(0)
            return 42
        """)
    client.send(python=python)
    assert last_tool_text(client) == "[done]"
    client.send(python="asyncio.run(answer())")
    assert last_tool_text(client) == "42\n"
    return client._finish()


def test_recovers_from_python_errors(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: python
    python = code("""
        answer = 41


        def fail():
            raise ValueError("boom")


        fail()
        """)
    client.send(python=python)
    output = last_tool_text(client)
    assert client.transcript[-1]["result"]["isError"] is False
    assert output.startswith("Traceback (most recent call last):\n")
    assert "<mcp-console:python:" in output
    assert "in fail\n" in output
    assert output.endswith("ValueError: boom\n")
    # fmt: python
    python = code("""
        compile_partial = 9
        await missing()
        """)
    client.send(python=python)
    output = last_tool_text(client)
    assert output.startswith("Traceback (most recent call last):\n")
    assert "<mcp-console:python:" in output
    assert output.endswith("SyntaxError: 'await' outside function\n")
    client.send(python='"compile_partial" in globals()')
    assert last_tool_text(client) == "False\n"

    client.send(python="nul_state = 42\0")
    output = last_tool_text(client)
    assert client.transcript[-1]["result"]["isError"] is False
    assert "SyntaxError" in output
    assert "null bytes" in output
    client.send(python="answer")
    assert last_tool_text(client) == "41\n"
    return client._finish()


def test_routes_python_input(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()

    # fmt: python
    python = code("""
        name = input("name> ")
        name
        """)
    client.send(python=python)
    assert last_tool_text(client) == '[input requested: "name> "]\n[stdin needed]'
    client.send(stdin="Ada\n")
    assert last_tool_text(client) == "'Ada'\n"

    # fmt: python
    python = code("""
        color = input("color> ")
        color
        """)
    client.send(python=python, stdin="blue\n")
    assert last_tool_text(client) == ("[input requested: \"color> \"]\n'blue'\n")

    # fmt: python
    python = code("""
        import sys

        direct = sys.stdin.readline()
        direct
        """)
    client.send(python=python, stdin="fd 0\n")
    assert last_tool_text(client) == "'fd 0\\n'\n"
    return client._finish()


def test_python_debugger_input(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()

    # fmt: python
    python = code("""
        import pdb

        debug_value = 41
        pdb.set_trace()
        debug_value += 1
        """)
    client.send(python=python)
    output = last_tool_text(client)
    assert output.count('[input requested: "(Pdb) "]') == 1, output
    assert output.endswith("\n[stdin needed]"), output

    client.send(stdin="p debug_value\n")
    output = last_tool_text(client)
    assert output.count('[input requested: "(Pdb) "]') == 1, output
    assert "41\n" in output, output
    assert output.endswith("\n[stdin needed]"), output

    client.send(stdin="continue\n")
    assert last_tool_text(client) == "[done]"
    client.send(python="debug_value")
    assert last_tool_text(client) == "42\n"
    return client._finish()


def test_restarts_after_python_bridge_failure(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        python_worker_marker <- TRUE
        Sys.setenv(RETICULATE_PYTHON = "/mcp-console-missing-python")
        invisible(suppressMessages(base::trace(
          "py_discover_config",
          tracer = quote(base::signalCondition(base::structure(
            base::list(message = "synthetic interrupt", call = NULL),
            class = c("interrupt", "condition")
          ))),
          print = FALSE,
          where = asNamespace("reticulate")
        )))
        """)
    client.send(r=r)
    client.send(python="6 * 7")
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    bridge_failure = "Python bridge failed during R evaluation\n"
    python_failure = (
        "Error in py_discover_config(required_module, use_environment) : \n"
        "  Python specified in RETICULATE_PYTHON "
        "(/mcp-console-missing-python) does not exist\n"
    )
    worker_failure = (
        "[worker sideband read failed: worker sideband closed]\n"
        "[worker stopped: in-memory state lost]\n"
        "[starting new worker]\n"
        "[idle]"
    )
    output = result["content"][0]["text"]
    expected = {
        bridge_failure + python_failure + worker_failure,
        python_failure + bridge_failure + worker_failure,
    }
    assert output in expected, output
    result["content"][0]["text"] = bridge_failure + python_failure + worker_failure
    client.send(r='exists("python_worker_marker", inherits = FALSE)')
    assert last_tool_text(client) == "[1] FALSE\n"
    client.send(python="6 * 7")
    assert last_tool_text(client) == "42\n"
    return client._finish()


def last_tool_text(client: McpClient) -> str:
    return client.transcript[-1]["result"]["content"][0]["text"]


if __name__ == "__main__":
    run_this_suite(__file__)
