#!/usr/bin/env -S uv run --script

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from _support import McpClient, Transcript, code, run_this_suite


PLATFORMS = {"darwin"}


def test_evaluates_queries_in_a_persistent_catalog(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as home:
        environment = os.environ.copy()
        environment["HOME"] = home
        environment["R_LIBS"] = inherited_r_libraries()
        environment["RETICULATE_PYTHON"] = sys.executable
        client = McpClient(binary, ("serve",), environment)
        client.initialize_and_list_tools()
        sql = code(r"""
            CREATE TABLE answers AS SELECT CAST(42 AS INTEGER) AS answer
            """)
        client.call_tool("send", sql=sql)
        output = last_tool_text(client)
        assert output == "[done]", output

        sql = code(r"""
            SELECT answer FROM answers
            """)
        client.call_tool("send", sql=sql)
        normalize_answer(client)
        return client.finish()


def test_recovers_from_sql_errors(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    sql = code(r"""
        SELECT * FROM table_that_does_not_exist
        """)
    client.call_tool("send", sql=sql)
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    output = result["content"][0]["text"]
    assert output.startswith("Error: "), output
    assert "Catalog Error:" in output, output
    assert "table_that_does_not_exist" in output, output
    result["content"][0]["text"] = "<DuckDB catalog error>\n"

    sql = code(r"""
        SELECT CAST(42 AS INTEGER) AS answer
        """)
    client.call_tool("send", sql=sql)
    normalize_answer(client)
    return client.finish()


def last_tool_text(client: McpClient) -> str:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    return result["content"][0]["text"]


def normalize_answer(client: McpClient) -> None:
    assert last_tool_text(client) == " answer\n     42\n"
    client.transcript[-1]["result"]["content"][0]["text"] = "<SQL query result>\n"


def inherited_r_libraries() -> str:
    r_home = os.environ.get("R_HOME")
    rscript = Path(r_home, "bin", "Rscript") if r_home else "Rscript"
    source = code(r"""
        writeLines(.libPaths())
        """)
    output = subprocess.run(
        [rscript, "--vanilla", "-e", source],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return os.pathsep.join(output.splitlines())


if __name__ == "__main__":
    run_this_suite(__file__)
