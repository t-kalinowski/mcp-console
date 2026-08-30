#!/usr/bin/env -S uv run --script

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import McpClient, Transcript, code, r_test_environment, run_this_suite

PLATFORMS = {"darwin"}


def test_routes_sql_cells_to_a_selected_dbi_connection(
    binary: Path,
) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()

    client.send(
        sql=(
            "CREATE TABLE managed_values AS "
            "SELECT CAST(42 AS INTEGER) AS value"
        )
    )
    assert last_tool_text(client) == "[done]"

    # Use a different DBI backend so the test covers the generic connection
    # path rather than another DuckDB connection.
    # fmt: r
    r = code(r"""
        custom_connection <- DBI::dbConnect(
          RSQLite::SQLite(),
          ":memory:"
        )
        console_sql_connection(custom_connection)
        cat(
          "selected: ", identical(sql_connection(), custom_connection), "\n",
          "valid: ", DBI::dbIsValid(console_sql_connection()), "\n",
          sep = ""
        )
        """)
    client.send(r=r, requirements={"r": ["RSQLite"]})
    assert last_tool_text(client) == "selected: TRUE\nvalid: TRUE\n"

    client.send(
        sql=(
            "CREATE TABLE custom_values ("
            "label TEXT NOT NULL, value INTEGER NOT NULL)"
        )
    )
    assert last_tool_text(client) == "[done]"
    client.send(
        sql="INSERT INTO custom_values VALUES ('a', 2), ('b', 5)"
    )
    assert last_tool_text(client) == "[done]"

    sql = code(r"""
        -- The selected DBI backend handles this query.
        SELECT label, value
        FROM custom_values
        ORDER BY label
        """)
    client.send(sql=sql)
    preview = last_tool_text(client)
    assert '"a"' in preview and "2" in preview
    assert '"b"' in preview and "5" in preview
    assert "42" not in preview

    client.send(
        sql=(
            "INSERT INTO custom_values VALUES ('c', 11) "
            "RETURNING label, value"
        )
    )
    preview = last_tool_text(client)
    assert '"c"' in preview and "11" in preview

    # fmt: r
    r = code(r"""
        selected <- console_sql_connection()
        console_sql_connection(NULL)
        cat(
          "restored: ", !identical(sql_connection(), selected), "\n",
          sep = ""
        )
        invisible(DBI::dbDisconnect(selected))
        """)
    client.send(r=r)
    assert last_tool_text(client) == "restored: TRUE\n"

    client.send(sql="SELECT value FROM managed_values")
    preview = last_tool_text(client)
    assert "42" in preview
    assert '"a"' not in preview
    return client._finish()


def last_tool_text(client: McpClient) -> str:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    return result["content"][0]["text"]


if __name__ == "__main__":
    run_this_suite(__file__)
