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
            INSERT INTO answers VALUES (7)
            """)
        client.call_tool("send", sql=sql)
        output = last_tool_text(client)
        assert output == "[done]", output

        sql = code(r"""
            SELECT answer FROM answers
            ORDER BY answer DESC
            """)
        client.call_tool("send", sql=sql)
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
    return client.finish()


def test_previews_schema_and_exact_values(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    r = code(r"""
        invisible(options(
          width = 20L,
          max.print = 1L,
          digits = 2L,
          scipen = -9L,
          OutDec = ",",
          cli.num_colors = 256L,
          cli.unicode = FALSE,
          pillar.advice = TRUE,
          pillar.bidi = TRUE,
          pillar.bold = TRUE,
          pillar.min_title_chars = 1000L,
          pillar.width = 20L,
          pillar.print_max = 1L,
          pillar.max_extra_cols = 0L,
          pillar.superdigit_sep = "XYZ",
          pillar.subtle = TRUE
        ))
        """)
    client.call_tool("send", r=r)
    assert last_tool_text(client) == "[done]"

    sql = code(r"""
        SELECT
          CAST(NULL AS VARCHAR) AS missing,
          CAST('9223372036854775807' AS BIGINT) AS big,
          CAST(
            '12345678901234567890123456789.123456789'
            AS DECIMAL(38, 9)
          ) AS exact,
          CAST([1, NULL, 3] AS INTEGER[]) AS items,
          {'name': 'Ada', 'active': true} AS person
        """)
    client.call_tool("send", sql=sql)
    values = last_tool_text(client)

    sql = code(r"""
        SELECT
          1 AS column_name_01_is_deliberately_long,
          2 AS column_name_02_is_deliberately_long,
          3 AS column_name_03_is_deliberately_long,
          4 AS column_name_04_is_deliberately_long,
          5 AS column_name_05_is_deliberately_long,
          6 AS column_name_06_is_deliberately_long,
          7 AS column_name_07_is_deliberately_long,
          8 AS column_name_08_is_deliberately_long,
          9 AS column_name_09_is_deliberately_long,
          10 AS column_name_10_is_deliberately_long,
          11 AS column_name_11_is_deliberately_long,
          12 AS column_name_12_is_deliberately_long
        """)
    client.call_tool("send", sql=sql)
    long_names = last_tool_text(client)

    sql = code(r"""
        SELECT
          CAST(NULL AS INTEGER) AS id,
          CAST(NULL AS DECIMAL(10, 2)) AS amount,
          CAST(NULL AS VARCHAR[]) AS tags
        WHERE FALSE
        """)
    client.call_tool("send", sql=sql)
    empty = last_tool_text(client)
    transcript = client.finish()

    assert "missing" in values
    assert "<int64>" in values
    assert "decimal128(38, 9)" in values
    assert "NULL" in values
    assert "9223372036854775807" in values
    assert "12345678901234567890123456789.123456789" in values
    assert "[1, NULL, 3]" in values
    assert "Ada" in values and "true" in values
    for column in range(1, 13):
        assert f"column_name_{column:02d}_is_deliberately_long" in long_names
    assert "abbreviated names" in long_names
    assert "0 rows" in empty
    assert "id" in empty and "amount" in empty and "tags" in empty
    assert "int32" in empty
    assert "decimal128(10, 2)" in empty
    return transcript


def test_bounds_query_previews_without_materializing_results(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    sql = code(r"""
        SELECT
          repeat('a', 1000) AS c01,
          repeat('b', 1000) AS c02,
          repeat('c', 1000) AS c03,
          repeat('d', 1000) AS c04,
          repeat('e', 1000) AS c05,
          repeat('f', 1000) AS c06,
          repeat('g', 1000) AS c07,
          repeat('h', 1000) AS c08,
          repeat('i', 1000) AS c09,
          repeat('j', 1000) AS c10,
          repeat('k', 1000) AS c11,
          repeat('l', 1000) AS c12,
          repeat('m', 1000) AS c13,
          repeat('n', 1000) AS c14
        FROM range(21)
        """)
    client.call_tool("send", sql=sql)
    wide = last_tool_text(client)

    sql = code(r"""
        SELECT value
        FROM range(1000000000000) AS values(value)
        WHERE value % 97 = 0
        """)
    client.call_tool("send", sql=sql, timeout_ms=1000)
    large = last_tool_text(client)
    transcript = client.finish()

    assert len(wide.encode("utf-8")) <= 12 * 1024
    assert "[additional rows omitted]" in wide
    assert "[2 additional columns omitted]" in wide
    assert "[cell values truncated to 160 characters]" in wide
    assert "a" * 161 not in wide
    assert large != "\n[running]"
    assert len(large.encode("utf-8")) <= 12 * 1024
    assert "[additional rows omitted]" in large
    return transcript


def test_keeps_repeated_previews_deterministic(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    sql = code(r"""
        CREATE VIEW wide_values AS SELECT
          repeat('😀漢é', 2000) AS c01,
          repeat('界🚀', 2000) AS c02,
          repeat('á', 5000) AS c03,
          repeat('🙂', 5000) AS c04,
          repeat('字', 5000) AS c05,
          repeat('é', 5000) AS c06,
          repeat('🚧', 5000) AS c07,
          repeat('語', 5000) AS c08,
          repeat('ñ', 5000) AS c09,
          repeat('🌐', 5000) AS c10,
          repeat('ß', 5000) AS c11,
          repeat('文', 5000) AS c12,
          repeat('extra', 2000) AS c13
        FROM range(21)
        """)
    client.call_tool("send", sql=sql)
    assert last_tool_text(client) == "[done]"

    sql = code(r"""
        SELECT * FROM wide_values
        """)
    outputs = []
    for _ in range(3):
        client.call_tool("send", sql=sql)
        outputs.append(last_tool_text(client))
    transcript = client.finish()

    assert outputs[0] == outputs[1] == outputs[2]
    assert len(outputs[0].encode("utf-8")) <= 12 * 1024
    assert outputs[0].startswith("# A tibble:")
    for entry in transcript[-3:]:
        entry["result"]["content"][0]["text"] = "<same bounded preview>\n"
    return transcript


def last_tool_text(client: McpClient) -> str:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    return result["content"][0]["text"]


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
