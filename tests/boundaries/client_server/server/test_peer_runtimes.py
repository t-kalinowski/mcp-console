#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code, normalize_python_traceback_paths
from support.records import Transcript, TranscriptWithCompanions
from support.requirements import NO_R, R_RUNTIME, requires
from support.resolvers import bare_runtime_environment, resolve_managed_python
from support.suites import run_this_suite


@executions(DIRECT, SANDBOXED)
def test_python_preserves_exact_queued_stdin(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(python="input()", stdin="caf\u00e9\0tail\n")
        assert (
            last_result_text(client)
            == "[input requested: \"\"]\n'caf\u00e9\\x00tail'\n"
        )
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(R_RUNTIME)
def test_r_activation_does_not_initialize_python_or_sql(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        # fmt: r
        r = code("""
            stopifnot(!any(c("reticulate", "duckdb", "DBI") %in% loadedNamespaces()))
            answer <- 41L
            answer + 1L
            """)
        client.send(r=r)
        assert last_result_text(client) == "[1] 42\n"
        client.send(python="r.answer + 1")
        assert last_result_text(client) == "42\n", last_result_text(client)
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_interrupts_python_input_without_losing_state(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(python="answer = 41\ninput('ready> ')")
        assert (
            last_result_text(client)
            == '[input requested: "ready> "]\n[waiting for stdin]'
        )
        client.send(control="interrupt")
        assert "KeyboardInterrupt" in last_result_text(client)
        client.send(python="answer + 1")
        assert last_result_text(client) == "42\n", last_result_text(client)
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(R_RUNTIME)
def test_routes_interrupts_across_nested_languages(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(python="answer = 41")
        for cell in (
            {"r": 'readline("R> ")'},
            {"python": 'r.readline("nested R> ")'},
            {"r": "reticulate::py_eval(\"input('nested Python> ')\")"},
        ):
            client.send(**cell)
            assert "[waiting for stdin]" in last_result_text(client)
            result = client.send(control="interrupt")
            result["content"][0]["text"] = normalize_python_traceback_paths(
                last_result_text(client)
            )
            client.send(python="answer + 1")
            assert last_result_text(client) == "42\n", last_result_text(client)
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_python_requirements_preserve_live_objects_and_input(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(requirements={"python": ["packaging"]})
        assert last_result_text(client) == "[prepared]"
        # fmt: python
        python = code("""
            import importlib.metadata
            import os
            import packaging

            original_pid = os.getpid()
            original_object = object()
            original_identity = id(original_object)
            packaging_version = importlib.metadata.version("packaging")
            packaging_version
            """)
        client.send(python=python)
        version = last_result_text(client).strip().strip("'")
        assert version != "23.2", version
        client.transcript[-1]["result"]["content"][0]["text"] = (
            "<loaded packaging version>\n"
        )
        client.send(stdin="queued\n")
        result = client.send(requirements={"python": ["packaging==23.2"]})
        error = result["content"][0]["text"]
        assert result["isError"] is True, result
        assert f"Cannot replace loaded packaging {version} with 23.2" in error, error
        result["content"][0]["text"] = error.replace(
            version, "<loaded packaging version>"
        )
        client.send(requirements={"python": ["py-yaml12"]})
        assert last_result_text(client) == "[prepared]"
        # fmt: python
        python = code("""
            import yaml12
            import defusedxml

            assert os.getpid() == original_pid
            assert id(original_object) == original_identity
            assert importlib.metadata.version("packaging") == packaging_version
            input()
            """)
        client.send(python=python)
        assert last_result_text(client).endswith("[input requested: \"\"]\n'queued'\n")
        client.send(control="restart")
        client.send(python="import yaml12, defusedxml\n'original_object' in globals()")
        assert last_result_text(client) == "False\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_python_does_not_activate_r(binary: Path, execution: Execution) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        # fmt: python
        python = code("""
            import ctypes
            import os
            import sys

            answer = 41
            original_pid = os.getpid()
            assert not hasattr(ctypes.CDLL(None), "Rf_initialize_R")
            assert "duckdb" not in sys.modules
            answer + 1
            """)
        client.send(python=python)
        assert last_result_text(client) == "42\n", last_result_text(client)
        return client.finish()


@requires(NO_R)
@executions(DIRECT, SANDBOXED)
def test_records_python_without_r_dependencies(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        with McpClient(
            binary, execution.serve(), current_directory=workspace
        ) as client:
            client.initialize_and_list_tools()
            client.send(python="6 * 7")
            assert last_result_text(client) == "42\n", last_result_text(client)
            transcript = client.finish()
        session = next((workspace / ".agents/console/sessions").iterdir())
        quarto = (session / "transcript.qmd").read_text()
        assert "knitr:" not in quarto and "\nir:" not in quarto, quarto
        assert "eval: false" in quarto, quarto
        assert "```{python}" in quarto, quarto
        return TranscriptWithCompanions(
            transcript=transcript, companions={"qmd": quarto}
        )


@requires(NO_R)
@executions(DIRECT, SANDBOXED)
def test_no_r_user_selected_python_is_bare(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        python = resolve_managed_python(binary, execution, directory)
        environment = bare_runtime_environment(os.environ.copy(), directory)
        environment["RETICULATE_PYTHON"] = str(python)
        with McpClient(binary, execution.serve(), environment) as client:
            client.initialize_and_list_tools()
            properties = client.transcript[-1]["result"]["tools"][0]["inputSchema"][
                "properties"
            ]
            assert "requirements" not in properties
            client.send(python="answer = 41\nanswer + 1")
            assert last_result_text(client) == "42\n", last_result_text(client)
            client.send(sql="SELECT 42 AS answer")
            assert "42" in last_result_text(client), last_result_text(client)
            return client.finish()


@requires(NO_R)
@executions(DIRECT, SANDBOXED)
def test_python_and_sql_without_r(binary: Path, execution: Execution) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(sql="CREATE TABLE answers AS SELECT 42 AS answer")
        client.send(sql="SELECT answer FROM answers")
        assert "42" in last_result_text(client), last_result_text(client)
        # fmt: python
        python = code("""
            import ctypes
            import os

            original_pid = os.getpid()
            answer = 41
            import numpy as np
            import pandas as pd

            registered = pd.DataFrame({"value": np.array([20, 22])})
            sql_connection().register("registered", registered)
            assert not hasattr(ctypes.CDLL(None), "Rf_initialize_R")
            assert sql_connection().execute("SELECT answer FROM answers").fetchone() == (42,)
            answer + 1
            """)
        client.send(python=python)
        assert last_result_text(client) == "42\n", last_result_text(client)
        client.send(sql="SELECT sum(value) AS answer FROM registered")
        assert "42" in last_result_text(client), last_result_text(client)
        client.send(
            python="import sqlite3\nconsole_sql_connection(sqlite3.connect(':memory:'))"
        )
        client.send(sql="SELECT 7 AS temporary_answer")
        assert "7" in last_result_text(client)
        client.send(python="console_sql_connection(None)")
        client.send(sql="SELECT answer FROM answers")
        assert "42" in last_result_text(client), last_result_text(client)
        client.send(r="1 + 1")
        assert "R is unavailable" in last_result_text(client)
        client.send(requirements={"r": ["praise"]})
        assert "R is unavailable" in last_result_text(client)
        client.send(python="(os.getpid() == original_pid, answer)")
        assert last_result_text(client) == "(True, 41)\n"
        client.send(sql="SELECT answer FROM answers")
        assert "42" in last_result_text(client), last_result_text(client)
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(R_RUNTIME)
def test_reticulate_tracks_console_python_activation(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(r="invisible(reticulate::py_config())")
        assert last_result_text(client) == "[done]"
        client.send(requirements={"python": ["py-yaml12"]})
        assert last_result_text(client) == "[prepared]"
        # fmt: r
        r = code("""
            stopifnot(
              identical(
                reticulate::py_config()$python,
                reticulate::import("sys")$executable
              ),
              "py-yaml12" %in% reticulate::py_require()$packages
            )
            py$answer <- 42L
            """)
        client.send(r=r)
        assert last_result_text(client) == "[done]", last_result_text(client)
        client.send(python="answer")
        assert last_result_text(client) == "42\n", last_result_text(client)
        return client.finish()


@requires(NO_R)
@executions(DIRECT, SANDBOXED)
def test_no_r_sql_interrupt_and_worker_crash(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        # Managed input belongs to the main worker thread, including a SQL UDF.
        client.send(sql="SET threads = 1")
        client.send(sql="CREATE TABLE answers AS SELECT 42 AS answer")
        client.send(requirements={"python": ["py-yaml12"]})
        assert last_result_text(client) == "[prepared]"
        # fmt: python
        python = code("""
            def sql_gate(value):
                input("SQL gate> ")
                return value


            _ = sql_connection().create_function("sql_gate", sql_gate, ["BIGINT"], "BIGINT")
            """)
        client.send(python=python)
        client.send(sql="SELECT sql_gate(answer) FROM answers")
        assert "[waiting for stdin]" in last_result_text(client), last_result_text(
            client
        )
        client.send(control="interrupt")
        assert "KeyboardInterrupt" in last_result_text(client)
        client.send(sql="SELECT answer FROM answers")
        assert "42" in last_result_text(client), last_result_text(client)
        client.send(
            sql="SELECT sum(sqrt(a.i + b.i)) FROM range(1000000000) a(i), range(1000000000) b(i)",
            timeout_ms=100,
        )
        assert last_result_text(client).endswith("[running; poll with an empty send]")
        client.send(control="interrupt")
        assert last_result_text(client) == "Error: Query interrupted\n", (
            last_result_text(client)
        )
        client.send(sql="SELECT answer FROM answers")
        assert "42" in last_result_text(client), last_result_text(client)
        client.send(python="import os\nos._exit(17)")
        output = last_result_text(client)
        assert "[worker exited with status 17]" in output, output
        assert "[starting new worker]" in output, output
        client.send(python="import yaml12\n'sql_gate' in globals()")
        assert last_result_text(client) == "False\n"
        client.send(
            sql="SELECT count(*) AS count FROM information_schema.tables WHERE table_name = 'answers'"
        )
        assert "0" in last_result_text(client)
        return client.finish()


@requires(NO_R)
@executions(DIRECT, SANDBOXED)
def test_no_r_extension_preparation_uses_candidate_provider(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        # fmt: python
        python = code("""
            import os
            import sys

            original_pid = os.getpid()
            original_executable = sys.executable
            answer = 41
            assert "duckdb" not in sys.modules
            """)
        client.send(python=python)
        client.send(
            requirements={
                "python": ["duckdb==1.4.4"],
                "duckdb": ["not_a_real_duckdb_extension"],
            }
        )
        output = last_result_text(client)
        assert "/v1.4.4/" in output, output
        assert client.transcript[-1]["result"]["isError"] is True
        # fmt: python
        python = code("""
            assert os.getpid() == original_pid
            assert sys.executable == original_executable
            answer + 1
            """)
        client.send(python=python)
        assert last_result_text(client) == "42\n", last_result_text(client)
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
