#!/usr/bin/env -S uv run --script

import os
import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from support.assertions import last_result_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.linux_sandbox import retain_system_bwrap
from support.normalization import code
from support.records import Transcript, TranscriptWithCompanions
from boundaries.client_server.python.test_without_r import (
    environment as without_r_environment,
)
from support.resolvers import bare_runtime_environment, resolve_managed_python
from support.suites import run_this_suite


def no_r_environment(directory: Path) -> dict[str, str]:
    commands = directory / "commands"
    commands.mkdir()
    uv = shutil.which("uv")
    assert uv is not None, "uv is required"
    (commands / "uv").symlink_to(uv)
    return without_r_environment(commands)


@contextmanager
def no_r_client(binary: Path, execution: Execution):
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        with McpClient(
            binary, execution.serve(), no_r_environment(workspace), workspace
        ) as client:
            yield client


@executions(DIRECT, SANDBOXED)
def test_records_python_without_r_dependencies(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        with McpClient(
            binary, execution.serve(), no_r_environment(workspace), workspace
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


@executions(DIRECT, SANDBOXED)
def test_no_r_user_selected_python_is_bare(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment = no_r_environment(directory)
        python = resolve_managed_python(
            binary, execution, directory, environment=environment
        )
        environment = bare_runtime_environment(environment, directory / "library")
        environment["PATH"] = str(directory / "empty-path")
        Path(environment["PATH"]).mkdir()
        retain_system_bwrap(Path(environment["PATH"]))
        environment["RETICULATE_PYTHON"] = str(python)
        with McpClient(binary, execution.serve(), environment) as client:
            client.initialize_and_list_tools()
            properties = client.transcript[-1]["result"]["tools"][0]["inputSchema"][
                "properties"
            ]
            assert properties["requirements"]["properties"] == {
                "action": {"type": "string", "enum": ["get"]}
            }, properties["requirements"]
            client.send(
                # fmt: python
                python=code("""
                    answer = 41
                    answer + 1
                    """)
            )
            assert last_result_text(client) == "42\n", last_result_text(client)
            client.send(sql="SELECT 42 AS answer")
            assert "42" in last_result_text(client), last_result_text(client)
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_python_and_sql_without_r(binary: Path, execution: Execution) -> Transcript:
    with no_r_client(binary, execution) as client:
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
            # fmt: python
            python=code("""
                import sqlite3

                console_sql_connection(sqlite3.connect(":memory:"))
                """)
        )
        client.send(sql="SELECT 7 AS temporary_answer")
        assert "7" in last_result_text(client)
        client.send(python="console_sql_connection(None)")
        client.send(sql="SELECT answer FROM answers")
        assert "42" in last_result_text(client), last_result_text(client)
        client.send(r="1 + 1")
        assert (
            last_result_text(client)
            == "R cells are unavailable in Python sessions without R"
        ), client.transcript[-1]
        client.send(requirements={"r": ["praise"]})
        assert (
            last_result_text(client)
            == "R requirements are unavailable in Python sessions without R"
        ), client.transcript[-1]
        client.send(python="(os.getpid() == original_pid, answer)")
        assert last_result_text(client) == "(True, 41)\n"
        client.send(sql="SELECT answer FROM answers")
        assert "42" in last_result_text(client), last_result_text(client)
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_no_r_sql_interrupt_and_worker_crash(
    binary: Path, execution: Execution
) -> Transcript:
    with no_r_client(binary, execution) as client:
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
        client.send(
            # fmt: python
            python=code("""
                import os

                os._exit(17)
                """)
        )
        output = last_result_text(client)
        assert "[worker exited with status 17]" in output, output
        assert "[starting new worker]" in output, output
        client.send(
            # fmt: python
            python=code("""
                import yaml12

                "sql_gate" in globals()
                """)
        )
        assert last_result_text(client) == "False\n"
        client.send(
            sql="SELECT count(*) AS count FROM information_schema.tables WHERE table_name = 'answers'"
        )
        assert "0" in last_result_text(client)
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_no_r_extension_preparation_uses_candidate_provider(
    binary: Path, execution: Execution
) -> Transcript:
    with no_r_client(binary, execution) as client:
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
            control="restart",
            requirements={
                "action": "set",
                "python": ["numpy", "pandas", "duckdb==1.4.4"],
                "duckdb": ["not_a_real_duckdb_extension"],
            },
        )
        output = last_result_text(client)
        platform = re.search(r"/v1\.4\.4/([^/]+)/not_a_real_duckdb_extension", output)
        assert platform is not None, output
        assert f"platform={platform[1]}&" in output, output
        assert client.transcript[-1]["result"]["isError"] is True
        client.transcript[-1]["result"]["content"][0]["text"] = output.replace(
            platform[1], "<duckdb platform>"
        )
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
