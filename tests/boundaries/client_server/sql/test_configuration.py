#!/usr/bin/env -S uv run --script

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text, wait_for_evaluation_output
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.requirements import POSIX, R, SQL, requires
from support.suites import run_this_suite


@requires(POSIX, R, SQL)
@executions(SANDBOXED)
def test_selected_python_preserves_preinstalled_extensions_with_r_present(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        python = workspace / ".venv/bin/python"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", python.parent.parent],
            check=True,
        )
        subprocess.run(
            ["uv", "pip", "install", "--python", python, "duckdb==1.4.4"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                python,
                "-I",
                "-c",
                "import duckdb; connection = duckdb.connect(); connection.install_extension('sqlite')",
            ],
            check=True,
            capture_output=True,
        )
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "python": ".venv/bin/python",
                    "cache": "console",
                    "sql": {"provider": "python"},
                }
            )
        )
        env = dict(os.environ, XDG_CACHE_HOME=str(workspace / "cache"))
        with McpClient(binary, execution.serve(), env, workspace) as client:
            client.initialize_and_list_tools()
            for restart in (False, True):
                if restart:
                    client.send(control="restart")
                client.expect(r="stopifnot(is.environment(globalenv()))")
                client.expect(
                    # fmt: python
                    python=code("""
                        import duckdb

                        with duckdb.connect() as native:
                            _ = native.execute("SET autoinstall_known_extensions = false; LOAD sqlite")
                            native_cache = native.execute(
                                "SELECT current_setting('extension_directory')"
                            ).fetchone()
                        managed = sql_connection()
                        _ = managed.execute("SET autoinstall_known_extensions = false; LOAD sqlite")
                        assert (
                            managed.execute("SELECT current_setting('extension_directory')").fetchone()
                            == native_cache
                        )
                        """),
                )
                client.expect(
                    "Success\n-------\n[0 rows]\n",
                    sql="LOAD sqlite",
                )
                inspected = client.send(requirements={"action": "get"})
                assert inspected["structuredContent"]["requirements"]["python"] == []
                assert inspected["structuredContent"]["requirements"]["duckdb"] == []
            client.finish()
    return [
        {"selected_python_native_extensions_with_r_present": ["startup", "restart"]}
    ]


@requires(R, SQL)
@executions(DIRECT, SANDBOXED)
def test_prepares_extensions_for_python_with_r_present(
    binary: Path, execution: Execution
) -> Transcript:
    for before_launch in (True, False):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            cache = workspace / "extensions"
            env = dict(os.environ, MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY=str(cache))
            config = workspace / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps({"cache": "host", "sql": {"provider": "python"}})
            )
            with McpClient(binary, execution.serve(), env, workspace) as client:
                client.initialize_and_list_tools()
                prepared = client.send(
                    requirements={
                        "action": "set",
                        "python": ["duckdb==1.4.4"],
                        "duckdb": ["fts"] if before_launch else [],
                    }
                )
                assert not prepared.get("isError"), prepared
                client.expect(
                    r='stopifnot(as.character(packageVersion("duckdb")) != "1.4.4")'
                )
                client.expect(
                    # fmt: python
                    python=code("""
                        import duckdb
                        import os

                        assert duckdb.__version__ == "1.4.4"
                        pid = os.getpid()
                        managed = sql_connection()
                        _ = managed.execute("CREATE TABLE retained AS SELECT 42 AS answer")
                        """),
                )
                if not before_launch:
                    prepared = client.send(requirements={"duckdb": ["fts"]})
                    assert not prepared.get("isError"), prepared
                client.expect(
                    "Success\n-------\n[0 rows]\n",
                    sql="SET autoinstall_known_extensions = false; LOAD fts",
                )
                client.expect(
                    "Success\n-------\n[0 rows]\n",
                    requirements={"python": ["six"], "duckdb": ["sqlite"]},
                    sql="LOAD sqlite",
                )
                client.expect(
                    # fmt: python
                    python=code("""
                        import six

                        assert os.getpid() == pid
                        assert sql_connection() is managed
                        assert managed.execute("SELECT answer FROM retained").fetchone() == (42,)
                        assert managed.execute(
                            "SELECT bool_and(installed AND loaded) FROM duckdb_extensions() "
                            "WHERE extension_name IN ('fts', 'sqlite_scanner')"
                        ).fetchone() == (True,)
                        """),
                )
                assert list(cache.glob("v1.4.4/**/fts.duckdb_extension"))
                assert list(cache.glob("v1.4.4/**/sqlite_scanner.duckdb_extension"))
                failed = client.send(
                    control="restart",
                    requirements={
                        "action": "set",
                        "python": ["six"],
                        "duckdb": ["fts"],
                    },
                )
                assert failed.get("isError"), failed
                failure = failed["content"][0]["text"]
                assert "include duckdb in requirements.python" in failure, failed
                client.expect(
                    python="assert os.getpid() == pid; assert sql_connection() is managed; "
                    'assert managed.execute("SELECT answer FROM retained").fetchone() == (42,)'
                )
                client.send(control="restart")
                client.expect(
                    "Success\n-------\n[0 rows]\n",
                    sql="SET autoinstall_known_extensions = false; LOAD fts; LOAD sqlite",
                )
                client.expect(
                    python='import duckdb; assert duckdb.__version__ == "1.4.4"'
                )
                client.finish()
    return [
        {
            "python_engine_extensions_with_r_present": [
                "before_launch",
                "live",
                "combined_python_addition",
                "restart",
            ],
            "missing_candidate_duckdb": failure,
        }
    ]


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_selects_python_with_r_present(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "sandbox": {
                        "environment": {
                            "MCP_CONSOLE_SQL_SETTINGS": "invalid ambient SQL settings"
                        }
                    },
                    "sql": {
                        "provider": "python",
                        "options": {"threads": 2, "memory_limit": "256MB"},
                    },
                }
            )
        )
        with McpClient(
            binary, execution.serve(), current_directory=workspace
        ) as client:
            client.initialize_and_list_tools()
            client.expect(r="stopifnot(is.environment(globalenv()))")
            inspected = client.send(requirements={"action": "get"})
            assert "duckdb" in inspected["structuredContent"]["requirements"]["python"]
            client.expect(
                "Success\n-------\n[0 rows]\n",
                sql="SET autoinstall_known_extensions = false; LOAD sqlite",
            )
            client.expect(
                # fmt: python
                python=code("""
                    import duckdb

                    managed = sql_connection()
                    assert isinstance(managed, duckdb.DuckDBPyConnection)
                    assert managed.execute("SELECT current_setting('threads')").fetchone() == (2,)
                    assert managed.execute("SELECT current_setting('memory_limit')").fetchone() == (
                        "244.1 MiB",
                    )
                    _ = managed.execute("CREATE TABLE configured AS SELECT 42 AS answer")
                    """),
            )
            client.expect("answer\n------\n42\n", sql="SELECT answer FROM configured")
            client.finish()
    return [
        {
            "python_default_with_r_present": True,
            "threads": 2,
            "memory_limit": "244.1 MiB",
        }
    ]


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_persists_database_and_captured_options(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    for provider in ("r", "python"):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            config = workspace / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            sql = {
                "provider": provider,
                "database": "analysis.duckdb",
                "options": {"threads": 2, "memory_limit": "256MB"},
            }
            config.write_text(json.dumps({"sql": sql}))
            with McpClient(
                binary,
                execution.serve(
                    *(
                        ("--writable-root", str(workspace))
                        if execution == SANDBOXED
                        else ()
                    ),
                    "-c",
                    "sql.options.threads=3",
                    "-c",
                    "sql.options.threads=2",
                ),
                current_directory=workspace,
            ) as client:
                client.initialize_and_list_tools()
                wait_for_evaluation_output(
                    client,
                    None,
                    "configured persistent catalog creation",
                    completion_timeout_seconds=client.response_timeout,
                    **(
                        {"requirements": {"python": ["duckdb"]}}
                        if provider == "python"
                        else {}
                    ),
                    sql="CREATE TABLE durable AS SELECT 42 AS answer",
                )
                assert "Error" not in last_tool_text(client), client.transcript[-1]
                # A live session retains its captured settings across generations.
                config.write_text(
                    json.dumps(
                        {
                            "sql": {
                                "provider": "python" if provider == "r" else "r",
                                "options": {"threads": 9},
                            }
                        }
                    )
                )
                client.send(control="restart")
                assert "Error" not in last_tool_text(client), client.transcript[-1]
                assert_native_catalog(client, provider)
                client.finish()
            assert (workspace / "analysis.duckdb").is_file()
            config.write_text(json.dumps({"sql": {**sql, "read_only": True}}))
            with McpClient(
                binary, execution.serve(), current_directory=workspace
            ) as client:
                client.initialize_and_list_tools()
                assert_native_catalog(client, provider)
                client.send(sql="INSERT INTO durable VALUES (7)")
                assert "read-only" in last_tool_text(client).lower(), client.transcript[
                    -1
                ]
                client.send(sql="SELECT answer FROM durable")
                assert "42" in last_tool_text(client), client.transcript[-1]
                client.finish()
            records.append(
                {
                    "provider": provider,
                    "persistent_relative_database": True,
                    "captured_restart_settings": True,
                    "read_only": True,
                }
            )
    return records


def assert_native_catalog(client: McpClient, provider: str) -> None:
    if provider == "r":
        client.expect(
            # fmt: r
            r=code("""
                connection <- sql_connection()
                stopifnot(
                  inherits(connection, "duckdb_connection"),
                  DBI::dbGetQuery(connection, "SELECT current_setting('threads')")[[1L]] == 2,
                  DBI::dbGetQuery(connection, "SELECT current_setting('memory_limit')")[[1L]] ==
                    "244.1 MiB",
                  DBI::dbGetQuery(connection, "SELECT answer FROM durable")[[1L]] == 42
                )
                """),
        )
    else:
        client.expect(
            # fmt: python
            python=code("""
                import duckdb

                connection = sql_connection()
                assert isinstance(connection, duckdb.DuckDBPyConnection)
                assert connection.execute("SELECT current_setting('threads')").fetchone() == (2,)
                assert connection.execute("SELECT current_setting('memory_limit')").fetchone() == (
                    "244.1 MiB",
                )
                assert connection.execute("SELECT answer FROM durable").fetchone() == (42,)
                """),
        )


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_preserves_native_selection_and_reset(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        with McpClient(
            binary,
            execution.serve(
                *(
                    ("--writable-root", str(workspace))
                    if execution == SANDBOXED
                    else ()
                ),
                "-c",
                "sql.provider=python",
                "-c",
                "sql.database=managed.duckdb",
            ),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                # fmt: python
                python=code("""
                    import sqlite3
                    import pandas as pd

                    managed = sql_connection()
                    frame = pd.DataFrame({"answer": [42]})
                    managed.register("registered", frame)
                    _ = managed.execute("CREATE TABLE durable AS SELECT * FROM registered")
                    user = sqlite3.connect(":memory:")
                    _ = user.execute("CREATE TABLE selected AS SELECT 7 AS answer")
                    _ = user.execute("BEGIN")
                    console_sql_connection(user)
                    assert sql_connection() is user
                    """),
            )
            client.send(sql="SELECT answer FROM selected")
            assert "7" in last_tool_text(client), client.transcript[-1]
            client.expect(
                python="assert user.in_transaction; console_sql_connection(None); assert sql_connection() is managed"
            )
            client.expect(
                # fmt: r
                r=code("""
                    message <- tryCatch(sql_connection(), error = conditionMessage)
                    stopifnot(grepl("provider is Python", message, fixed = TRUE))
                    user_r <- DBI::dbConnect(duckdb::duckdb(), dbdir = ":memory:")
                    invisible(DBI::dbExecute(user_r, "CREATE TABLE selected AS SELECT 9 AS answer"))
                    DBI::dbBegin(user_r)
                    console_sql_connection(user_r)
                    stopifnot(identical(sql_connection(), user_r))
                    invisible()
                    """),
            )
            client.send(sql="SELECT answer FROM selected")
            assert "9" in last_tool_text(client), client.transcript[-1]
            client.expect(
                r="DBI::dbRollback(user_r); console_sql_connection(NULL); stopifnot(DBI::dbIsValid(user_r)); invisible()"
            )
            client.expect(
                python="assert sql_connection() is managed; assert user.in_transaction; assert managed.execute('SELECT answer FROM durable').fetchone() == (42,)"
            )
            client.send(sql="SELECT * FROM frame")
            assert "does not exist" in last_tool_text(client), client.transcript[-1]
            client.expect(python="console_sql_connection(user)")
            client.expect(r="console_sql_connection(user_r); invisible()")
            client.expect(python="console_sql_connection(None)")
            client.expect(
                r='stopifnot(grepl("provider is Python", tryCatch(sql_connection(), error = conditionMessage), fixed = TRUE))'
            )
            client.send(sql="SELECT answer FROM durable")
            assert "42" in last_tool_text(client), client.transcript[-1]
            client.finish()
    return [
        {
            "native_connections_and_transactions_retained": True,
            "reset_from_both_languages": True,
            "explicit_python_registration": True,
            "r_preview_with_python_file_owner": True,
        }
    ]


@requires(SQL, R)
@executions(DIRECT, SANDBOXED)
def test_selected_r_connection_does_not_initialize_python(
    binary: Path, execution: Execution
) -> Transcript:
    from boundaries.client_server.python.test_setup import deferred_selection_client

    with deferred_selection_client(
        binary, execution.serve("-c", "sql.provider=python")
    ) as client:
        client.expect(
            # fmt: r
            r=code("""
                original_python <- Sys.getenv("RETICULATE_PYTHON", unset = NA_character_)
                Sys.setenv(RETICULATE_PYTHON = "/mcp-console-missing-python")
                retained_pid <- Sys.getpid()
                retained_state <- new.env()
                retained_state$answer <- 42L
                native <- DBI::dbConnect(duckdb::duckdb(), dbdir = ":memory:")
                invisible(DBI::dbExecute(native, "CREATE TABLE selected AS SELECT 1 AS answer"))
                DBI::dbBegin(native)
                invisible(DBI::dbExecute(native, "UPDATE selected SET answer = 42"))
                console_sql_connection(native)
                stopifnot(DBI::dbGetQuery(native, "SELECT answer FROM selected")[[1L]] == 42)
                invisible()
                """),
        )
        client.expect(
            "# A tibble: 1 × 1\n   answer\n  <int32>\n1      42\n",
            sql="SELECT answer FROM selected",
        )
        client.expect(
            "# A tibble: 1 × 1\n   answer\n  <int32>\n1      43\n",
            sql="UPDATE selected SET answer = answer + 1 RETURNING answer",
        )
        client.expect(
            # fmt: r
            r=code("""
                stopifnot(
                  Sys.getpid() == retained_pid,
                  retained_state$answer == 42L,
                  identical(sql_connection(), native),
                  !reticulate::py_available(initialize = FALSE),
                  DBI::dbGetQuery(native, "SELECT answer FROM selected")[[1L]] == 43
                )
                DBI::dbRollback(native)
                stopifnot(DBI::dbGetQuery(native, "SELECT answer FROM selected")[[1L]] == 1)
                if (is.na(original_python)) {
                  Sys.unsetenv("RETICULATE_PYTHON")
                } else {
                  Sys.setenv(RETICULATE_PYTHON = original_python)
                }
                """),
        )
        # Starting Python later must preserve the earlier native R selection.
        client.expect(python="import sqlite3")
        client.expect(
            "# A tibble: 1 × 1\n   answer\n  <int32>\n1       1\n",
            sql="SELECT answer FROM selected",
        )
        client.expect(
            # fmt: python
            python=code("""
                user = sqlite3.connect(":memory:")
                _ = user.execute("CREATE TABLE selected AS SELECT 7 AS answer")
                console_sql_connection(user)
                """),
        )
        client.expect("answer\n------\n7\n", sql="SELECT answer FROM selected")
        client.expect(
            r="stopifnot(identical(sql_connection(), native), DBI::dbIsValid(native))"
        )
        client.finish()
    return [
        {
            "selected_r_connection_with_missing_python": True,
            "python_remains_uninitialized": True,
            "native_identity_transaction_and_worker_state_retained": True,
            "selection_survives_later_python_initialization": True,
            "later_python_selection_takes_precedence": True,
        }
    ]


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_r_selection_preserves_live_global_lookup(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(
        binary, execution.serve("-c", "sql.provider=r", "-c", "sql.options.threads=2")
    ) as client:
        client.initialize_and_list_tools()
        client.expect(python="python_first = True")
        client.expect(
            r="live <- data.frame(answer = 1); collision <- data.frame(answer = 99)"
        )
        client.expect(
            sql="CREATE VIEW live_view AS SELECT * FROM live; CREATE TABLE collision AS SELECT 42 AS answer"
        )
        client.expect(r="live <- data.frame(answer = 7)")
        client.expect(
            # fmt: r
            r=code("""
                connection <- sql_connection()
                stopifnot(
                  DBI::dbGetQuery(connection, "SELECT answer FROM live_view")[[1L]] == 7,
                  DBI::dbGetQuery(connection, "SELECT answer FROM collision")[[1L]] == 42,
                  DBI::dbGetQuery(connection, "SELECT current_setting('threads')")[[1L]] == 2
                )
                """),
        )
        client.finish()
    return [
        {
            "explicit_r_live_view_rebinding": True,
            "catalog_precedence": True,
            "python_first": True,
        }
    ]


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_sql_only_uses_hidden_python_provider(
    binary: Path, execution: Execution
) -> Transcript:
    environment = os.environ | {"MCP_CONSOLE_LANGUAGES": "sql"}
    with McpClient(
        binary,
        execution.serve("-c", "sql.provider=python", "-c", "sql.options.threads=2"),
        environment=environment,
    ) as client:
        client.initialize_and_list_tools()
        properties = client.transcript[-1]["result"]["tools"][0]["inputSchema"][
            "properties"
        ]
        assert (
            "sql" in properties and "r" not in properties and "python" not in properties
        ), properties
        client.expect(
            "threads\n-------\n2\n",
            requirements={"python": ["duckdb"]},
            sql="SELECT current_setting('threads') AS threads",
        )
        client.send(control="restart")
        client.expect(
            "threads\n-------\n2\n", sql="SELECT current_setting('threads') AS threads"
        )
        client.finish()
    return [{"sql_only_hidden_python": True, "restart_selection": True}]


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_invalid_engine_option_does_not_fall_back(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    for provider in ("r", "python"):
        with McpClient(
            binary,
            execution.serve(
                "-c",
                f"sql.provider={provider}",
                "-c",
                "sql.options.memory_limit=invalid-unit",
            ),
        ) as client:
            client.initialize_and_list_tools()
            wait_for_evaluation_output(
                client,
                None,
                "invalid managed engine option",
                completion_timeout_seconds=client.response_timeout,
                **(
                    {"requirements": {"python": ["duckdb"]}}
                    if provider == "python"
                    else {}
                ),
                sql="SELECT 42 AS answer",
            )
            assert "Memory must have a number" in last_tool_text(client), (
                client.transcript[-1]
            )
            client.expect(r="stopifnot(1 + 1 == 2)")
            client.expect(python="assert 1 + 1 == 2")
            client.finish()
        records.append(
            {
                "provider": provider,
                "invalid_engine_value_visible": True,
                "unrelated_cells_usable": True,
            }
        )
    return records


@requires(SQL, POSIX)
@executions(DIRECT, SANDBOXED)
def test_configured_warmup_keeps_discovery_and_first_cell_ordering(
    binary: Path, execution: Execution
) -> Transcript:
    from boundaries.client_server.sql.test_connections import startup_sql_client

    for provider in ("r", "python"):
        with startup_sql_client(
            binary,
            execution,
            True,
            "observe",
            sql={
                "provider": provider,
                "database": ":memory:",
                "options": {"threads": 2},
            },
        ) as (client, started, release):
            try:
                started.wait(
                    "configured managed connection opened before first cell", timeout=60
                )
            except AssertionError:
                client.send(timeout_ms=0)
                raise AssertionError(
                    f"{provider} warmup checkpoint missing: {last_tool_text(client)}"
                ) from None
            client.request("ping")
            client.request("tools/list")
            pending = client.start_send(
                sql="INSERT INTO startup_catalog SELECT 7 AS answer", timeout_ms=0
            )
            client.receive(pending)
            assert "running; poll with an empty send" in last_tool_text(client), (
                client.transcript[-1]
            )
            release.release()
            client.send(timeout_ms=30_000)
            assert "Error" not in last_tool_text(client), client.transcript[-1]
            if provider == "r":
                client.expect(
                    r='stopifnot(DBI::dbGetQuery(sql_connection(), "SELECT count(*) FROM startup_catalog WHERE answer = 7")[[1L]] == 1)'
                )
            else:
                client.expect(
                    python='assert sql_connection().execute("SELECT count(*) FROM startup_catalog WHERE answer = 7").fetchone() == (1,)'
                )
            client.finish()
    return [
        {
            "first_constructor_receives_options": True,
            "discovery_during_warmup": True,
            "early_sql_runs_once": True,
            "providers": ["r", "python"],
        }
    ]


@requires(SQL)
@executions(SANDBOXED)
def test_database_path_does_not_grant_write_access(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        for provider in ("r", "python"):
            with McpClient(
                binary,
                execution.serve(
                    "-c", f"sql.provider={provider}", "-c", "sql.database=denied.duckdb"
                ),
                current_directory=workspace,
            ) as client:
                client.initialize_and_list_tools()
                wait_for_evaluation_output(
                    client,
                    None,
                    "unwritable managed database",
                    completion_timeout_seconds=client.response_timeout,
                    **(
                        {"requirements": {"python": ["duckdb"]}}
                        if provider == "python"
                        else {}
                    ),
                    sql="CREATE TABLE denied AS SELECT 42",
                )
                output = last_tool_text(client)
                assert "Cannot open file" in output and (
                    "Operation not permitted" in output or "Permission denied" in output
                ), client.transcript[-1]
                client.finish()
            assert not (workspace / "denied.duckdb").exists()
    return [
        {
            "database_path_does_not_grant_write_access": True,
            "providers": ["r", "python"],
        }
    ]


@requires(SQL, POSIX)
@executions(DIRECT, SANDBOXED)
def test_missing_explicit_r_provider_does_not_use_python(
    binary: Path, execution: Execution
) -> Transcript:
    from boundaries.client_server.sql.test_without_r import environment
    from support.linux_sandbox import retain_system_bwrap

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        native_bin = root / "bin"
        native_bin.mkdir()
        retain_system_bwrap(native_bin, os.environ.get("PATH"))
        (native_bin / "uv").symlink_to(shutil.which("uv"))
        with McpClient(
            binary,
            execution.serve("-c", "sql.provider=r"),
            environment(native_bin),
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.send(sql="SELECT 42 AS answer")
            assert "R is unavailable in this session" in last_tool_text(client), (
                client.transcript[-1]
            )
            client.expect(python="assert 1 + 1 == 2")
            client.finish()
    return [
        {
            "missing_explicit_r_provider_reported": True,
            "no_python_sql_fallback": True,
            "python_cells_usable": True,
        }
    ]


if __name__ == "__main__":
    run_this_suite(__file__)
