#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _support import (
    FifoCheckpoint,
    McpClient,
    Transcript,
    code,
    r_test_environment,
    run_this_suite,
    stop_client,
    wait_for_worker_file,
)

PLATFORMS = {"darwin"}


def test_routes_sql_cells_to_a_selected_dbi_connection(
    binary: Path,
) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()

    client.send(sql="CREATE TABLE managed_values AS SELECT 'managed' AS origin")
    assert last_tool_text(client) == "[done]"

    # fmt: r
    r = code(r"""
        sqlite <- DBI::dbConnect(RSQLite::SQLite(), ":memory:")
        console_sql_connection(connection = sqlite)
        cat(
          "selected: ",
          identical(sql_connection(), sqlite),
          "\n",
          "valid: ",
          DBI::dbIsValid(sql_connection()),
          "\n",
          sep = ""
        )
        """)
    client.send(r=r, requirements={"r": ["RSQLite"]})
    assert last_tool_text(client) == "selected: TRUE\nvalid: TRUE\n"

    # fmt: r
    r = code(r"""
        selected <- sql_connection()
        message <- tryCatch(
          console_sql_connection("not a connection"),
          error = conditionMessage
        )
        cat(
          "rejected: ",
          message,
          "\n",
          "unchanged: ",
          identical(sql_connection(), selected),
          "\n",
          sep = ""
        )
        """)
    client.send(r=r)
    assert last_tool_text(client) == (
        "rejected: `connection` must be a valid DBIConnection or NULL\n"
        "unchanged: TRUE\n"
    )

    client.send(
        sql=("CREATE TABLE custom_values (label TEXT NOT NULL, value INTEGER NOT NULL)")
    )
    assert last_tool_text(client) == "[done]"
    client.send(sql="INSERT INTO custom_values VALUES ('a', 2), ('b', 5)")
    assert last_tool_text(client) == "[done]"

    client.send(r="previous_warn <- getOption('warn'); options(warn = 2); invisible()")
    assert last_tool_text(client) == "[done]"
    client.send(sql="INSERT INTO custom_values VALUES ('RETURNING', 7)")
    assert last_tool_text(client) == "[done]"
    client.send(
        sql=(
            "WITH incoming(label, value) AS (VALUES ('cte', 9)) "
            "INSERT INTO custom_values SELECT label, value FROM incoming"
        )
    )
    assert last_tool_text(client) == "[done]"
    client.send(sql="PRAGMA user_version = 3")
    assert last_tool_text(client) == "[done]"
    client.send(r="invisible(options(warn = previous_warn))")
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
    assert '"RETURNING"' in preview and "7" in preview
    assert '"cte"' in preview and "9" in preview
    assert '"managed"' not in preview

    client.send(sql="SELECT missing FROM missing_values")
    assert last_tool_text(client) == "Error: no such table: missing_values\n"

    client.send(sql="SELECT value FROM custom_values WHERE FALSE")
    preview = last_tool_text(client)
    assert "value" in preview and "<int32>" in preview, preview
    assert "[0 rows]" in preview

    sql = code(r"""
        WITH RECURSIVE sequence(value) AS (
          SELECT 1
          UNION ALL
          SELECT value + 1 FROM sequence WHERE value < 25
        )
        SELECT value FROM sequence
        """)
    client.send(sql=sql)
    preview = last_tool_text(client)
    assert "20" in preview
    assert "[additional rows omitted]" in preview

    client.send(
        sql=("INSERT INTO custom_values VALUES ('c', 11) RETURNING label, value")
    )
    preview = last_tool_text(client)
    assert '"c"' in preview and "11" in preview

    # fmt: r
    r = code(r"""
        selected <- sql_connection()
        invisible(DBI::dbDisconnect(selected))
        cat(
          "disconnected: ",
          !DBI::dbIsValid(selected),
          "\n",
          sep = ""
        )
        """)
    client.send(r=r)
    assert last_tool_text(client) == "disconnected: TRUE\n"

    client.send(sql="SELECT label FROM custom_values")
    assert last_tool_text(client) == (
        "Error: The selected SQL connection is no longer valid; "
        "call console_sql_connection(NULL) to restore DuckDB\n"
    )

    # fmt: r
    r = code(r"""
        console_sql_connection(NULL)
        cat(
          "restored: ",
          !identical(sql_connection(), selected),
          "\n",
          sep = ""
        )
        """)
    client.send(r=r)
    assert last_tool_text(client) == "restored: TRUE\n"

    client.send(sql="SELECT origin FROM managed_values")
    preview = last_tool_text(client)
    assert '"managed"' in preview
    assert '"a"' not in preview
    return client._finish()


def test_routes_sql_cells_to_a_selected_python_dbapi_connection(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()

    client.send(sql="CREATE TABLE managed_values AS SELECT 'managed' AS origin")
    assert last_tool_text(client) == "[done]"

    # fmt: python
    python = code("""
        import sqlite3


        class CursorOnlyConnection:
            def __init__(self, connection):
                self.connection = connection

            def cursor(self):
                return self.connection.cursor()


        sqlite = sqlite3.connect(":memory:")
        connection = CursorOnlyConnection(sqlite)
        console_sql_connection(connection)
        del sqlite
        del connection
        """)
    client.send(python=python)
    assert last_tool_text(client) == "[done]"

    # Invalid selections leave the current provider unchanged.
    # fmt: python
    python = code("""
        try:
            console_sql_connection(object())
        except TypeError as error:
            print(error)
        """)
    client.send(python=python)
    assert last_tool_text(client) == (
        "`connection` must provide a callable cursor() method or be None\n"
    )

    client.send(sql="CREATE TABLE custom_values (label TEXT, value INTEGER)")
    assert last_tool_text(client) == "[done]"
    client.send(sql="INSERT INTO custom_values VALUES ('a', 2), ('b', NULL)")
    assert last_tool_text(client) == "[done]"
    client.send(sql="SELECT label, value FROM custom_values ORDER BY label")
    preview = last_tool_text(client)
    assert "label" in preview and "value" in preview
    assert "'a'" in preview and "2" in preview
    assert "'b'" in preview and "NULL" in preview
    assert "managed" not in preview

    client.send(sql="SELECT missing FROM missing_values")
    assert last_tool_text(client) == "Error: no such table: missing_values\n"

    control_alias = "tab\theader\nnext"
    client.send(sql=f'SELECT 1 AS "{control_alias}"')
    preview = last_tool_text(client)
    assert "\t" not in preview and control_alias not in preview
    assert "\\t" in preview and "\\n" in preview
    assert max(map(display_width, preview.splitlines())) <= 200

    alias = "é" * 240
    client.send(sql=f'SELECT 1 AS "{alias}"')
    preview = last_tool_text(client)
    assert len(preview.encode("utf-8")) <= 12 * 1024, len(preview.encode("utf-8"))
    assert max(map(display_width, preview.splitlines())) <= 200
    assert alias not in preview

    columns = ", ".join(
        (f"replace(printf('%040d', 0), '0', '🐍') AS value_{index}")
        for index in range(12)
    )
    client.send(
        sql=(
            "WITH RECURSIVE sequence(value) AS ("
            "SELECT 1 UNION ALL SELECT value + 1 FROM sequence WHERE value < 20"
            f") SELECT {columns} FROM sequence"
        )
    )
    preview = last_tool_text(client)
    assert len(preview.encode("utf-8")) <= 12 * 1024, len(preview.encode("utf-8"))
    assert max(map(display_width, preview.splitlines())) <= 200

    sql = code("""
        WITH RECURSIVE sequence(value) AS (
          SELECT 1
          UNION ALL
          SELECT value + 1 FROM sequence WHERE value < 25
        )
        SELECT value FROM sequence
        """)
    client.send(sql=sql)
    preview = last_tool_text(client)
    assert "20" in preview
    assert "[additional rows omitted]" in preview

    # Driver-specific values may return arbitrary text from repr().
    # fmt: python
    python = code("""
        class ControlValue:
            def __repr__(self):
                return "tab\\tline\\nreturn\\rcontrol\\x01" + "\\t" * 80


        class ControlCursor:
            description = (("value",),)

            def execute(self, source):
                return self

            def fetchmany(self, size):
                return [(ControlValue(),)][:size]

            def close(self):
                pass


        class ControlConnection:
            def cursor(self):
                return ControlCursor()


        console_sql_connection(ControlConnection())
        """)
    client.send(python=python)
    assert last_tool_text(client) == "[done]"

    client.send(sql="CONTROL VALUE")
    preview = last_tool_text(client)
    assert all(control not in preview for control in ("\t", "\r", "\x01"))
    assert all(escape in preview for escape in ("\\t", "\\n", "\\r", "\\x01"))
    assert len(preview.splitlines()) == 4
    assert max(map(display_width, preview.splitlines())) <= 200
    assert "[cell values truncated to 160 characters]" in preview

    client.send(r="console_sql_connection(NULL); invisible()")
    assert last_tool_text(client) == "[done]"
    client.send(sql="SELECT origin FROM managed_values")
    preview = last_tool_text(client)
    assert '"managed"' in preview
    assert "'a'" not in preview

    client.send(python="console_sql_connection(sqlite3.connect(':memory:'))")
    assert last_tool_text(client) == "[done]"
    client.send(sql="SELECT 42 AS python_value")
    assert "42" in last_tool_text(client)

    client.send(python="console_sql_connection(None)")
    assert last_tool_text(client) == "[done]"
    client.send(sql="SELECT origin FROM managed_values")
    preview = last_tool_text(client)
    assert '"managed"' in preview
    assert "'a'" not in preview
    return client._finish()


def test_preserves_selected_python_duckdb_connection_state(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()

    # fmt: python
    python = code("""
        import duckdb

        connection = duckdb.connect(":memory:")
        connection.execute("CREATE TEMP TABLE before_selection AS SELECT 41 AS value")
        console_sql_connection(connection)
        del connection
        """)
    client.send(
        python=python,
        requirements={"python": ["duckdb==1.5.5"]},
    )
    assert last_tool_text(client) == "[done]"

    client.send(sql="SELECT value + 1 AS answer FROM before_selection")
    preview = last_tool_text(client)
    assert "answer" in preview and "42" in preview

    client.send(sql="CREATE TEMP TABLE later_state AS SELECT 'retained' AS value")
    assert "Error:" not in last_tool_text(client)
    client.send(sql="SELECT value FROM later_state")
    preview = last_tool_text(client)
    assert "value" in preview and "'retained'" in preview
    return client._finish()


def test_reports_python_dbapi_cursor_cleanup_failures(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()

    # fmt: python
    python = code("""
        class CleanupCursor:
            description = (("answer",),)

            def execute(self, source):
                if source == "ERROR":
                    raise RuntimeError("selected DB-API execution failure")
                return self

            def fetchmany(self, size):
                return [(42,)][:size]

            def close(self):
                raise RuntimeError("selected DB-API cleanup failure")


        class CleanupConnection:
            def cursor(self):
                return CleanupCursor()


        console_sql_connection(CleanupConnection())
        """)
    client.send(python=python)
    assert last_tool_text(client) == "[done]"

    client.send(sql="ANSWER")
    output = last_tool_text(client)
    assert "answer" in output and "42" in output
    assert "Error: selected DB-API cleanup failure" in output

    client.send(sql="ERROR")
    output = last_tool_text(client)
    execution = "Error: selected DB-API execution failure"
    cleanup = "Error: selected DB-API cleanup failure"
    assert execution in output and cleanup in output
    assert output.index(execution) < output.index(cleanup)
    return client._finish()


def test_recovers_when_python_dbapi_connection_raises_base_exception(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()

    # fmt: python
    python = code("""
        class RecoverableCursor:
            description = None

            def __init__(self, connection):
                self.connection = connection

            def execute(self, source):
                if source == "EXIT":
                    raise SystemExit("selected DB-API failure")
                self.description = (("answer",),)
                return self

            def fetchmany(self, size):
                return [(42,)][:size]

            def close(self):
                if self.connection.close_failure:
                    self.connection.close_failure = False
                    raise SystemExit("selected DB-API cleanup failure")


        class RecoverableConnection:
            def __init__(self):
                self.close_failure = True

            def cursor(self):
                return RecoverableCursor(self)


        console_sql_connection(RecoverableConnection())
        """)
    client.send(python=python)
    assert last_tool_text(client) == "[done]"

    client.send(sql="EXIT")
    output = last_tool_text(client)
    assert "SystemExit: selected DB-API failure" in output
    assert "SystemExit: selected DB-API cleanup failure" in output

    client.send(sql="ANSWER")
    preview = last_tool_text(client)
    assert "answer" in preview and "42" in preview
    return client._finish()


def test_allows_python_dbapi_callbacks_to_select_an_r_connection(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(binary, ("serve",), environment)
        checkpoints: list[FifoCheckpoint] = []
        passed = False
        try:
            client._initialize_and_list_tools()
            client.send(sql="CREATE TABLE managed_values AS SELECT 42 AS value")
            assert last_tool_text(client) == "[done]"

            # Create checkpoint paths inside the worker's writable directory.
            # fmt: r
            r = code(r"""
                callback_started <- tempfile("mcp-console-sql-callback-started-")
                callback_release <- tempfile("mcp-console-sql-callback-release-")
                cat(callback_started, callback_release, sep = "\n")
                """)
            client.send(r=r)
            setup = client.transcript[-1]["result"]
            paths = setup["content"][0]["text"].splitlines()
            assert len(paths) == 2, setup
            setup["content"][0]["text"] = "<callback started>\n<callback release>"
            started, release = [FifoCheckpoint(Path(path)) for path in paths]
            checkpoints.extend((started, release))

            # The SQLite UDF re-enters R while the Python DB-API provider is
            # evaluating the current cell, then selects managed DuckDB for
            # later SQL cells.
            # fmt: r
            r = code(r"""
                select_r_sql <- function() {
                  started <- fifo(callback_started, open = "wb", blocking = TRUE)
                  writeBin(charToRaw("1"), started)
                  close(started)
                  gate <- fifo(callback_release, open = "rb", blocking = TRUE)
                  stopifnot(identical(
                    readBin(gate, "raw", n = 1L),
                    charToRaw("1")
                  ))
                  close(gate)
                  console_sql_connection(NULL)
                  41L
                }
                invisible()
                """)
            client.send(r=r)
            output = last_tool_text(client)
            assert output == "[done]", output

            # fmt: python
            python = code("""
                import sqlite3

                connection = sqlite3.connect(":memory:")
                connection.create_function("select_r_sql", 0, r.select_r_sql)
                console_sql_connection(connection)
                """)
            client.send(python=python)
            output = last_tool_text(client)
            assert output == "[done]", output

            evaluation = client._start_send(
                sql="SELECT select_r_sql() AS callback_value",
                timeout_ms=0,
            )
            started.wait("Python DB-API callback entered R")
            client._receive(evaluation)
            assert evaluation["result"]["content"][0]["text"] == (
                "\n[running; poll with an empty send]"
            )

            release.release()
            client.send(timeout_ms=3_000)
            preview = last_tool_text(client)
            assert "callback_value" in preview and "41" in preview, preview

            client.send(sql="SELECT value FROM managed_values")
            preview = last_tool_text(client)
            assert "value" in preview and "42" in preview

            transcript = client._finish()
            passed = True
            return transcript
        finally:
            for checkpoint in checkpoints:
                checkpoint.close()
            if not passed:
                stop_client(client)


def test_interrupts_selected_python_dbapi_connection(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(binary, ("serve",), environment)
        passed = False
        try:
            client._initialize_and_list_tools()
            # fmt: python
            python = code("""
                import os
                import time
                from pathlib import Path


                class InterruptibleConnection:
                    def __init__(self):
                        self.description = None
                        self.rows = []

                    def cursor(self):
                        return self

                    def execute(self, source):
                        if source == "WAIT":
                            Path(
                                os.environ["TMPDIR"],
                                "python-sql-interrupt-started",
                            ).touch()
                            while True:
                                time.sleep(60)
                        self.description = (("answer",),)
                        self.rows = [(42,)]
                        return self

                    def fetchmany(self, size):
                        return self.rows[:size]


                console_sql_connection(InterruptibleConnection())
                """)
            client.send(python=python)
            assert last_tool_text(client) == "[done]"

            client.send(sql="WAIT", timeout_ms=0)
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            wait_for_worker_file(
                temporary_path,
                "python-sql-interrupt-started",
                client,
            )

            client.send(control="interrupt", timeout_ms=30_000)
            assert "KeyboardInterrupt" in last_tool_text(client)

            client.send(sql="ANSWER")
            preview = last_tool_text(client)
            assert "answer" in preview and "42" in preview
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_client(client)


def test_interrupts_python_dbapi_provider_probe(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(binary, ("serve",), environment)
        checkpoints: list[FifoCheckpoint] = []
        passed = False
        try:
            client._initialize_and_list_tools()

            # Create the checkpoint inside the worker's writable directory.
            # fmt: r
            r = code(r"""
                probe_started <- tempfile("mcp-console-sql-probe-started-")
                Sys.setenv(MCP_CONSOLE_SQL_PROBE_STARTED = probe_started)
                cat(probe_started)
                """)
            client.send(r=r)
            setup = client.transcript[-1]["result"]
            path = Path(setup["content"][0]["text"])
            setup["content"][0]["text"] = "<probe started>"
            started = FifoCheckpoint(path)
            checkpoints.append(started)

            # fmt: python
            python = code("""
                import os
                import signal
                import sys


                class ProbeConnection:
                    description = (("answer",),)

                    def cursor(self):
                        return self

                    def execute(self, source):
                        return self

                    def fetchmany(self, size):
                        return [(42,)][:size]


                def pause_provider_probe(frame, event, argument):
                    if event == "call" and frame.f_globals.get("__name__") == "_mcp_console_sql":
                        sys.settrace(None)
                        with open(
                            os.environ["MCP_CONSOLE_SQL_PROBE_STARTED"],
                            "wb",
                            buffering=0,
                        ) as checkpoint:
                            checkpoint.write(b"1")
                        signal.pause()
                    return pause_provider_probe


                console_sql_connection(ProbeConnection())
                sys.settrace(pause_provider_probe)
                """)
            client.send(python=python)
            assert last_tool_text(client) == "[done]"

            evaluation = client._start_send(sql="ANSWER", timeout_ms=0)
            started.wait("Python DB-API provider probe started")
            client._receive(evaluation)
            assert evaluation["result"]["content"][0]["text"] == (
                "\n[running; poll with an empty send]"
            )

            client.send(control="interrupt", timeout_ms=30_000)
            assert "KeyboardInterrupt" in last_tool_text(client)

            client.send(sql="ANSWER")
            preview = last_tool_text(client)
            assert "answer" in preview and "42" in preview
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            for checkpoint in checkpoints:
                checkpoint.close()
            if not passed:
                stop_client(client)


def test_recovers_when_python_sql_dispatch_trace_raises_system_exit(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()

    # fmt: python
    python = code("""
        import _mcp_console_sql
        import sys


        class StatefulConnection:
            description = (("answer",),)

            def __init__(self):
                self.answer = 42

            def cursor(self):
                return self

            def execute(self, source):
                return self

            def fetchmany(self, size):
                return [(self.answer,)][:size]


        dispatch_code = _mcp_console_sql.dispatch.__code__


        def exit_sql_dispatch(frame, event, argument):
            if event == "call" and frame.f_code is dispatch_code:
                raise SystemExit("selected SQL dispatch exit")
            return exit_sql_dispatch


        connection = StatefulConnection()
        console_sql_connection(connection)
        sys.settrace(exit_sql_dispatch)
        """)
    client.send(python=python)
    assert last_tool_text(client) == "[done]"

    client.send(sql="ANSWER")
    assert "SystemExit: selected SQL dispatch exit" in last_tool_text(client)

    client.send(python="connection.answer")
    assert last_tool_text(client) == "42\n"

    client.send(sql="ANSWER")
    preview = last_tool_text(client)
    assert "answer" in preview and "42" in preview
    return client._finish()


def test_recovers_when_r_provider_switch_trace_raises_system_exit(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()

    client.send(sql="CREATE TABLE managed_value AS SELECT 7 AS value")
    assert last_tool_text(client) == "[done]"

    # fmt: python
    python = code("""
        import _mcp_console_sql
        import sqlite3
        import sys


        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE python_value AS SELECT 42 AS value")
        console_sql_connection(connection)
        use_r_code = _mcp_console_sql.use_r.__code__


        def exit_use_r(frame, event, argument):
            if event == "call" and frame.f_code is use_r_code:
                raise SystemExit("R provider switch exit")
            return exit_use_r


        sys.settrace(exit_use_r)
        """)
    client.send(python=python)
    assert last_tool_text(client) == "[done]"

    client.send(r="console_sql_connection(NULL); invisible()")
    assert "SystemExit: R provider switch exit" in last_tool_text(client)

    client.send(
        python="connection.execute('SELECT value FROM python_value').fetchone()[0]"
    )
    assert last_tool_text(client) == "42\n"

    client.send(sql="SELECT value FROM python_value")
    preview = last_tool_text(client)
    assert "value" in preview and "42" in preview

    client.send(r="console_sql_connection(NULL); invisible()")
    assert last_tool_text(client) == "[done]"
    client.send(sql="SELECT value FROM managed_value")
    preview = last_tool_text(client)
    assert "value" in preview and "7" in preview
    return client._finish()


def last_tool_text(client: McpClient) -> str:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    return result["content"][0]["text"]


def display_width(text: str) -> int:
    return sum(
        0
        if unicodedata.combining(character)
        else 2
        if unicodedata.east_asian_width(character) in {"F", "W"}
        else 1
        for character in text
    )


if __name__ == "__main__":
    run_this_suite(__file__)
