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
        client._initialize_and_list_tools()
        sql = code(r"""
            CREATE TABLE answers AS SELECT CAST(42 AS INTEGER) AS answer
            """)
        client.send(sql=sql)
        output = last_tool_text(client)
        assert output == "[done]", output

        sql = code(r"""
            INSERT INTO answers VALUES (7)
            """)
        client.send(sql=sql)
        output = last_tool_text(client)
        assert output == "[done]", output

        sql = code(r"""
            SELECT answer FROM answers
            ORDER BY answer DESC
            """)
        client.send(sql=sql)
        return client._finish()


def test_queries_r_data_frames(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    r = code(r"""
        measurements <- data.frame(
          label = c("a", "b"),
          value = c(2L, 5L)
        )
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"

    sql = code(r"""
        SELECT label, value * 10 AS scaled
        FROM measurements
        ORDER BY label
        """)
    client.send(sql=sql)
    preview = last_tool_text(client)
    assert '"a"' in preview and "20" in preview
    assert '"b"' in preview and "50" in preview
    return client._finish()


def test_sql_views_follow_rebound_r_data_frames(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    r = code(r"""
        measurements <- data.frame(value = 2L)
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"

    sql = code(r"""
        CREATE VIEW live_measurements AS
        SELECT value FROM measurements
        """)
    client.send(sql=sql)
    assert last_tool_text(client) == "[done]"

    r = code(r"""
        measurements <- data.frame(value = 7L)
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"

    sql = code(r"""
        SELECT value FROM live_measurements
        """)
    client.send(sql=sql)
    preview = last_tool_text(client)
    assert preview.splitlines()[-1].split() == ["1", "7"]
    return client._finish()


def test_prefers_catalog_relations_over_r_data_frames(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    r = code(r"""
        values <- data.frame(origin = "r")
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"

    sql = code(r"""
        CREATE TABLE values AS SELECT 'sql' AS origin;
        SELECT origin FROM values
        """)
    client.send(sql=sql)
    preview = last_tool_text(client)
    assert '"sql"' in preview
    assert '"r"' not in preview
    return client._finish()


def test_scans_r_bindings_named_like_bridge_state(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    r = code(r"""
        connection <- data.frame(name = "connection")
        source <- data.frame(name = "source")
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"

    sql = code(r"""
        SELECT name FROM connection
        UNION ALL
        SELECT name FROM source
        ORDER BY name
        """)
    client.send(sql=sql)
    preview = last_tool_text(client)
    assert '"connection"' in preview
    assert '"source"' in preview
    return client._finish()


def test_exposes_catalog_as_lazy_r_relations(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    sql = code(r"""
        CREATE TABLE sql_values AS
        SELECT * FROM (VALUES ('a', 2), ('b', 5)) AS values(label, value);
        CREATE VIEW live_sql_values AS SELECT * FROM sql_values
        """)
    client.send(sql=sql)
    assert last_tool_text(client) == "[done]"

    r = code(r"""
        connection <- sql_connection()
        table_values <- dplyr::tbl(connection, "sql_values")
        lazy_values <- dplyr::tbl(connection, "live_sql_values") |>
          dplyr::mutate(doubled = value * 2L)
        cat(
          "same connection: ", identical(connection, sql_connection()), "\n",
          "lazy table: ", inherits(table_values, "tbl_lazy"), "\n",
          "lazy view: ", inherits(lazy_values, "tbl_lazy"), "\n",
          sep = ""
        )
        """)
    client.send(r=r)
    assert last_tool_text(client) == (
        "same connection: TRUE\nlazy table: TRUE\nlazy view: TRUE\n"
    )

    sql = code(r"""
        INSERT INTO sql_values VALUES ('c', 11)
        """)
    client.send(sql=sql)
    assert last_tool_text(client) == "[done]"

    r = code(r"""
        values <- lazy_values |>
          dplyr::arrange(label) |>
          dplyr::collect()
        writeLines(paste(values$label, values$value, values$doubled, sep = ":"))
        """)
    client.send(r=r)
    assert last_tool_text(client) == "a:2:4\nb:5:10\nc:11:22\n"
    return client._finish()


def test_keeps_connection_helper_after_clearing_r_workspace(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    sql = code(r"""
        CREATE TABLE retained_values AS
        SELECT * FROM (VALUES ('a', 2), ('b', 5)) AS values(label, value)
        """)
    client.send(sql=sql)
    assert last_tool_text(client) == "[done]"

    r = code(r"""
        rm(list = ls())
        values <- DBI::dbGetQuery(
          sql_connection(),
          "SELECT label, value FROM retained_values ORDER BY label"
        )
        writeLines(paste(values$label, values$value, sep = ":"))
        """)
    client.send(r=r)
    assert last_tool_text(client) == "a:2\nb:5\n"
    return client._finish()


def test_recovers_from_sql_errors(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    sql = code(r"""
        SELECT * FROM table_that_does_not_exist
        """)
    client.send(sql=sql)
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
    client.send(sql=sql)
    return client._finish()


def test_avoids_private_preview_name_collisions(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    sql = code(r"""
        CREATE TABLE __mcp_console_preview_e2 AS
        SELECT CAST(999 AS INTEGER) AS column_01
        """)
    client.send(sql=sql)
    assert last_tool_text(client) == "[done]"

    sql = code(r"""
        SELECT CAST(42 AS INTEGER) AS answer
        """)
    client.send(sql=sql)
    preview = last_tool_text(client)
    assert "42" in preview
    assert "999" not in preview

    sql = code(r"""
        SELECT column_01 AS catalog_value
        FROM __mcp_console_preview_e2
        """)
    client.send(sql=sql)
    assert "999" in last_tool_text(client)
    return client._finish()


def test_previews_schema_and_exact_values(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
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
    client.send(r=r)
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
    client.send(sql=sql)
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
    client.send(sql=sql)
    long_names = last_tool_text(client)

    sql = code(r"""
        SELECT
          CAST(NULL AS INTEGER) AS id,
          CAST(NULL AS DECIMAL(10, 2)) AS amount,
          CAST(NULL AS VARCHAR[]) AS tags
        WHERE FALSE
        """)
    client.send(sql=sql)
    empty = last_tool_text(client)
    transcript = client._finish()

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
    client._initialize_and_list_tools()
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
    client.send(sql=sql)
    wide = last_tool_text(client)

    sql = code(r"""
        SELECT value
        FROM range(1000000000000) AS values(value)
        WHERE value % 97 = 0
        """)
    client.send(sql=sql, timeout_ms=1000)
    large = last_tool_text(client)
    transcript = client._finish()

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
    client._initialize_and_list_tools()
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
    client.send(sql=sql)
    assert last_tool_text(client) == "[done]"

    sql = code(r"""
        SELECT * FROM wide_values
        """)
    outputs = []
    for _ in range(3):
        client.send(sql=sql)
        outputs.append(last_tool_text(client))
    transcript = client._finish()

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
