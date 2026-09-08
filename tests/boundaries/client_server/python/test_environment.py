#!/usr/bin/env -S uv run --script

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import assert_exact_interleaving, last_result_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.requirements import OLD_PYTHON, SYSTEM_PYTHON, requires
from support.suites import run_this_suite


@executions(DIRECT, SANDBOXED)
def test_preserves_configured_python_environment(
    binary: Path, execution: Execution
) -> Transcript:
    environment = os.environ.copy()
    environment["RETICULATE_PYTHON"] = "configured-by-user"
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        external_python_worker <- Sys.getpid()
        external_python_state <- 42L
        stopifnot(
          identical(
            Sys.getenv("RETICULATE_PYTHON", unset = NA_character_),
            "configured-by-user"
          )
        )
        "configured-by-user"
        """)
    client.send(r=r)
    assert last_result_text(client) == '[1] "configured-by-user"\n'
    disabled = (
        "managed Python requirements are disabled because the session uses a "
        "user-selected Python environment"
    )
    for call_shape in ({}, {"control": "restart"}):
        client.send(
            **call_shape,
            requirements={"python": ["numpy"]},
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        assert last_result_text(client) == disabled

    client.send(
        r="external_python_combined_side_effect <- TRUE",
        requirements={"python": ["numpy"]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    assert last_result_text(client) == disabled
    # Reach the same worker-originated resolver request used by reticulate's
    # managed hooks. The server must enforce the external-selection policy
    # even though those hooks are not installed for this worker.
    # fmt: r
    r = code(r"""
        environment_request <- jsonlite::toJSON(list(
          requirements = list(packages = I("numpy")),
          retained_requirements = list(packages = I("numpy"))
        ), auto_unbox = TRUE)
        environment_error <- tryCatch(
          .Call("mcp_console_resolve_python", environment_request),
          error = conditionMessage
        )
        version_request <- jsonlite::toJSON(list(
          constraints = I(">=3.11")
        ), auto_unbox = TRUE)
        version_error <- tryCatch(
          .Call("mcp_console_resolve_python_version", version_request),
          error = conditionMessage
        )
        cat(environment_error, version_error, sep = "\n")
        """)
    client.send(r=r)
    output = last_result_text(client)
    assert output == f"{disabled}\n{disabled}\n", repr(output)
    # fmt: r
    r = code(r"""
        stopifnot(
          identical(Sys.getpid(), external_python_worker),
          identical(external_python_state, 42L),
          !exists("external_python_combined_side_effect", inherits = FALSE),
          identical(
            Sys.getenv("RETICULATE_PYTHON", unset = NA_character_),
            "configured-by-user"
          )
        )
        42L
        """)
    client.send(r=r)
    assert last_result_text(client) == "[1] 42\n"
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_preserves_empty_python_environment(
    binary: Path, execution: Execution
) -> Transcript:
    environment = os.environ.copy()
    environment["RETICULATE_PYTHON"] = ""
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        Sys.getenv("RETICULATE_PYTHON", unset = NA_character_)
        """)
    client.send(r=r)
    assert last_result_text(client) == '[1] "managed"\n'
    return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(OLD_PYTHON)
def test_rejects_python_older_than_3_10(
    binary: Path, execution: Execution
) -> Transcript:
    interpreter = SYSTEM_PYTHON
    version = subprocess.run(
        (interpreter, "-c", "import sys; print(sys.version_info[:2])"),
        check=True,
        capture_output=True,
        text=True,
    )
    assert version.stdout.strip() == "(3, 9)", version.stdout

    environment = os.environ.copy()
    environment["RETICULATE_PYTHON"] = str(interpreter)
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    client.send(python="6 * 7")
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    bridge_failure = "Python bridge failed during R evaluation\n"
    version_failure = (
        "Error: MCP Console requires Python 3.10 or later; "
        "selected interpreter reports Python 3.9\n"
    )
    worker_failure = (
        "[worker sideband read failed: worker sideband closed]\n"
        "[worker exited with status 1]\n"
        "[worker stopped: in-memory state lost]\n"
        "[starting new worker]\n"
        "[idle]"
    )
    output = result["content"][0]["text"]
    assert output.endswith(worker_failure), output
    assert_exact_interleaving(
        output.removesuffix(worker_failure),
        bridge_failure,
        version_failure,
    )
    result["content"][0]["text"] = bridge_failure + version_failure + worker_failure
    return client.finish()


def managed_python_transcript(
    binary: Path, execution: Execution, configured: bool
) -> Transcript:
    environment = os.environ.copy()
    if configured:
        environment["RETICULATE_PYTHON"] = "managed"
    else:
        environment.pop("RETICULATE_PYTHON", None)
    uv = shutil.which("uv")
    assert uv is not None, "real uv is required for managed-Python tests"
    environment.pop("RETICULATE_UV", None)
    environment["UV_OFFLINE"] = "1"

    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
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
    assert last_result_text(client) == "[done]", client.transcript[-1]
    # fmt: python
    python = code("""
        import io
        import pandas as pd

        frame = pd.read_csv(io.StringIO("value\\n40\\n2\\n"))
        int(frame["value"].sum())
        """)
    client.send(python=python)
    output = last_result_text(client)
    assert output == "42\n", repr(output)
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_evaluates_with_default_managed_python(
    binary: Path, execution: Execution
) -> Transcript:
    return managed_python_transcript(binary, execution, configured=False)


@executions(DIRECT, SANDBOXED)
def test_evaluates_with_explicit_managed_python(
    binary: Path, execution: Execution
) -> Transcript:
    return managed_python_transcript(binary, execution, configured=True)


@executions(DIRECT, SANDBOXED)
def test_does_not_import_local_psutil_during_bootstrap(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        (directory / "psutil.py").write_text(
            "import builtins\n"
            "builtins.mcp_console_local_psutil_imported = True\n"
            "answer = 42\n",
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment.pop("RETICULATE_PYTHON", None)
        client = McpClient(
            binary,
            execution.serve(),
            environment,
            current_directory=directory,
        )
        client.initialize_and_list_tools()
        # fmt: python
        python = code("""
            import builtins
            import sys

            (
                40 + 2,
                hasattr(builtins, "mcp_console_local_psutil_imported"),
                "psutil" in sys.modules,
            )
            """)
        client.send(python=python)
        output = last_result_text(client)
        assert output == "(42, False, False)\n", repr(output)
        client.send(python="import psutil; psutil.answer")
        assert last_result_text(client) == "42\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_sends_python_cell_with_initial_requirements(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # fmt: python
    python = code("""
        import yaml12

        print(yaml12.__name__)
        """)
    client.send(
        python=python,
        requirements={"python": ["py-yaml12"]},
    )
    assert last_result_text(client) == "yaml12\n"
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_compacts_native_duckdb_progress_bar(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # fmt: python
    python = code(r"""
        import os
        import tempfile

        import duckdb

        connection = duckdb.connect()
        assert connection.execute(
            "SELECT "
            "current_setting('enable_progress_bar'), "
            "current_setting('enable_progress_bar_print'), "
            "current_setting('progress_bar_time')"
        ).fetchone() == (True, True, 2000)
        connection.execute("SET progress_bar_time = 1000000")
        connection.execute("SET threads = 1")
        row_count = 15_000_000
        # DuckDB 1.5.5 emits at least 100 native progress redraws while
        # processing this single-threaded physical table scan.
        connection.execute(
            "CREATE TABLE progress_rows AS "
            "SELECT CAST(value AS INTEGER) AS value "
            f"FROM range({row_count}) AS values(value)"
        )

        saved_stdout = os.dup(1)
        try:
            with tempfile.TemporaryFile() as capture:
                os.dup2(capture.fileno(), 1)
                connection.execute("SET progress_bar_time = 0")
                result = connection.execute(
                    "SELECT sum(hash(value)) FROM progress_rows"
                ).fetchone()
                capture.seek(0)
                progress = capture.read()
        finally:
            os.dup2(saved_stdout, 1)
            os.close(saved_stdout)

        assert result[0] is not None
        assert progress.count(b"\r") >= 100
        with os.fdopen(os.dup(1), "wb") as stdout:
            stdout.write(progress)
        """)
    client.send(
        python=python,
        requirements={"python": ["duckdb==1.5.5"]},
        timeout_ms=0,
    )
    assert last_result_text(client) == "\n[running; poll with an empty send]"

    client.send(timeout_ms=220_000)
    output = last_result_text(client)
    assert "\r" not in output, repr(output)
    final = output.rstrip()
    assert final.count("% ▕") == 1, repr(final)
    graphic, separator, elapsed = final.rpartition(" (")
    assert graphic.startswith("100% ▕"), repr(final)
    assert graphic.endswith("▏"), repr(final)
    assert separator and elapsed.endswith(" elapsed)"), repr(final)
    client.transcript[-1]["result"]["content"][0]["text"] = f"{graphic} (<elapsed>)\n"
    client.transcript[-1]["transcript_normalization"] = {
        "target": "result.content[0].text",
        "elapsed": "omitted",
        "trailing_progress_padding": "omitted",
    }
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_uses_200_column_default(binary: Path, execution: Execution) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
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
    output = last_result_text(client)
    assert output.startswith(
        "terminal columns: 200\npandas display.width: 200\nNumPy linewidth: 200\n"
    ), repr(output)
    for column in range(12):
        assert f"column_{column:02}" in output
    assert "..." not in output
    assert "[1 rows x 12 columns]" not in output
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_uses_200_column_default_after_r_initializes_python(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
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
    assert last_result_text(client) == (
        "R-first NumPy linewidth: 200\nR-first pandas display.width: 200\n"
    )
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_prints_requirements_with_host_uv_cache(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        environment = os.environ.copy()
        trusted_cache = temporary / "trusted-uv-cache"
        worker_cache = temporary / "worker-uv-cache"
        uv_record = temporary / "uv-environment.jsonl"
        real_uv = shutil.which("uv")
        assert real_uv is not None, "real uv is required"
        environment["RETICULATE_UV"] = str(
            Path(__file__).parents[3] / "fixtures" / "record_uv_environment"
        )
        environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
        environment["MCP_CONSOLE_TEST_UV_RECORD"] = str(uv_record)
        environment["MCP_CONSOLE_TEST_WORKER_UV_CACHE"] = str(worker_cache)
        environment["UV_CACHE_DIR"] = str(trusted_cache)
        environment["UV_DEFAULT_INDEX"] = "https://pypi.org/simple"
        environment["UV_OFFLINE"] = "1"
        client = McpClient(
            binary,
            execution.serve(),
            environment,
            current_directory=temporary,
        )
        client.initialize_and_list_tools()
        uv_record.write_text("", encoding="utf-8")
        # fmt: r
        r = code(r"""
            worker_cache <- Sys.getenv("MCP_CONSOLE_TEST_WORKER_UV_CACHE")
            Sys.setenv(
              UV_CACHE_DIR = worker_cache,
              UV_DEFAULT_INDEX = "file:///worker-selected-index"
            )
            reticulate::py_require("py-yaml12")
            invisible(reticulate::py_config())
            stopifnot(
              reticulate::py_module_available("yaml12"),
              identical(Sys.getenv("UV_CACHE_DIR"), worker_cache),
              identical(
                Sys.getenv("UV_DEFAULT_INDEX"),
                "file:///worker-selected-index"
              )
            )
            """)
        client.send(r=r)
        assert last_result_text(client) == "[done]", client.transcript[-1]
        records = [
            json.loads(line)
            for line in uv_record.read_text(encoding="utf-8").splitlines()
        ]
        assert records, "runtime managed resolution did not invoke uv"
        expected = {
            "UV_CACHE_DIR": str(trusted_cache),
            "UV_DEFAULT_INDEX": "https://pypi.org/simple",
            "UV_OFFLINE": None,
        }
        assert all(record == expected for record in records), records
        if execution == DIRECT:
            # Reticulate also checks the activated environment with a local
            # dry-run, which inherits worker settings and may create its cache.
            diagnostics = [
                json.loads(line)
                for line in Path(str(uv_record) + ".diagnostics")
                .read_text()
                .splitlines()
            ]
            assert len(diagnostics) == 1, diagnostics
            diagnostic = diagnostics[0]
            assert diagnostic["arguments"][:10] == [
                "pip",
                "install",
                "--dry-run",
                "--no-deps",
                "--color",
                "never",
                "--no-progress",
                "--no-build",
                "--no-config",
                "--python",
            ], diagnostic
            assert diagnostic["arguments"][11:] == ["numpy", "pandas", "py-yaml12"], (
                diagnostic
            )
            assert diagnostic["environment"] == {
                "UV_CACHE_DIR": str(worker_cache),
                "UV_DEFAULT_INDEX": "file:///worker-selected-index",
                "UV_OFFLINE": "1",
            }, diagnostic
        else:
            assert not worker_cache.exists(), worker_cache
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
