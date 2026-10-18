"""Retain one native SQL result and consume it through the public helpers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text, wait_for_evaluation_output
from support.client import McpClient
from support.execution import RUNTIME, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.requirements import R, SQL, requires
from support.suites import run_this_suite


@requires(SQL)
@executions(RUNTIME)
def test_python_consumes_preview_and_bounded_chunks(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(
        binary, execution.serve("-c", "python.managed.packages=[]")
    ) as client:
        client.initialize_and_list_tools()
        client.expect(
            # fmt: python
            python=code("""
                import sqlite3


                class CountCursor:
                    def __init__(self):
                        self.native = selected.native.cursor()
                        self.closed = False
                        self.fetches = []
                        selected.cursors.append(self)

                    def execute(self, source):
                        selected.executions += 1
                        self.native.execute(source)
                        return self

                    @property
                    def description(self):
                        return self.native.description

                    def fetchmany(self, n):
                        self.fetches.append(n)
                        return self.native.fetchmany(
                            min(n, 2) if getattr(selected, "short", False) else n
                        )

                    def fetchall(self):
                        self.fetches.append("all")
                        return self.native.fetchall()

                    def close(self):
                        self.closed = True
                        self.native.close()


                class CountConnection:
                    def __init__(self):
                        self.native = sqlite3.connect(":memory:")
                        self.executions = 0
                        self.cursors = []

                    def cursor(self):
                        return CountCursor()


                selected = CountConnection()
                _console.sql_connection(selected)
                """),
        )
        client.send(
            sql="""WITH RECURSIVE rows(i) AS (
            VALUES (0) UNION ALL SELECT i + 1 FROM rows WHERE i < 56
        ) SELECT i, 9223372036854775807 AS big, NULL AS missing,
            zeroblob(200) AS payload, 'original' AS c05, 6 AS c06,
            7 AS c07, 8 AS c08, 9 AS c09, 10 AS c10, 11 AS c11,
            12 AS c12, 'complete column' AS c13 FROM rows"""
        )
        assert "additional rows omitted" in last_tool_text(client)
        client.expect(
            "bounded chunks preserve every row and column\n",
            # fmt: python
            python=code("""
                result = _console.sql_last_result()
                assert result is _console.sql_last_result()
                assert selected.executions == 1
                cursor = selected.cursors[-1]
                assert cursor.fetches == [21] and not cursor.closed
                assert len(result.columns) == 13 and result.columns[-1] == "c13"
                assert result.fetch(0) == [] and cursor.fetches == [21]
                first = result.fetch(5)
                assert cursor.fetches == [21]
                second = result.fetch(19)
                assert cursor.fetches == [21, 3]
                third = result.fetch(33)
                assert cursor.fetches == [21, 3, 33]
                assert result.fetch(1) == [] and cursor.closed
                assert result.fetch() == []
                rows = first + second + third
                assert [row[0] for row in rows] == list(range(57))
                assert all(row[1] == 9223372036854775807 for row in rows)
                assert all(row[2] is None and row[3] == bytes(200) for row in rows)
                assert all(row[12] == "complete column" for row in rows)
                assert selected.executions == 1
                assert "pandas" not in __import__("sys").modules
                print("bounded chunks preserve every row and column")
                """),
        )
        client.send(sql="SELECT 1 AS id WHERE FALSE")
        client.expect(
            "empty results retain columns and release cursors\n",
            # fmt: python
            python=code("""
                empty = _console.sql_last_result()
                assert empty.columns == ("id",) and empty.fetch() == []
                assert selected.cursors[-1].closed
                try:
                    result.fetch(1)
                except RuntimeError as error:
                    assert "invalidated" in str(error)
                else:
                    raise AssertionError("previous result remained usable")
                print("empty results retain columns and release cursors")
                """),
        )
        client.expect(python="selected.short = True")
        client.send(
            sql="""WITH RECURSIVE rows(i) AS (
            VALUES (0) UNION ALL SELECT i + 1 FROM rows WHERE i < 56
        ) SELECT i FROM rows"""
        )
        client.expect(
            "short native batches do not imply exhaustion\n",
            # fmt: python
            python=code("""
                result = _console.sql_last_result()
                cursor = selected.cursors[-1]
                assert not cursor.closed and cursor.fetches == [21]
                first = result.fetch(3)
                second = result.fetch(10)
                assert len(first) == 3 and len(second) == 2
                assert not cursor.closed and cursor.fetches == [21, 1, 10]
                rest = result.fetch()
                assert first + second + rest == [(i,) for i in range(57)]
                assert cursor.closed
                print("short native batches do not imply exhaustion")
                """),
        )
        return client.finish()


@requires(R, SQL)
@executions(RUNTIME)
def test_r_duckdb_preserves_exact_batches(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.expect(
            # fmt: r
            r=code("""
                metrics <- new.env()
                metrics$executions <- 0L
                metrics$fetches <- numeric()
                suppressMessages(invisible(trace(
                  "dbSendQueryArrow",
                  where = asNamespace("DBI"),
                  print = FALSE,
                  tracer = quote(
                    .GlobalEnv$metrics$executions <- .GlobalEnv$metrics$executions + 1L
                  )
                )))
                suppressMessages(invisible(trace(
                  "dbFetchArrowChunk",
                  where = asNamespace("DBI"),
                  print = FALSE,
                  tracer = quote(
                    .GlobalEnv$metrics$fetches <- c(
                      .GlobalEnv$metrics$fetches,
                      list(...)$chunk_size
                    )
                  )
                )))
                """),
        )
        client.send(
            sql="""CREATE SEQUENCE executions;
            SELECT i::INTEGER AS id, nextval('executions') AS execution,
                9223372036854775807::BIGINT AS big,
                12345678901234567890123456789.123456789::DECIMAL(38,9) AS exact,
                NULL::VARCHAR AS missing, repeat('x', 1000) AS long_value,
                [1, NULL, 3] AS items, {'label': 'original'} AS person,
                9 AS c09, 10 AS c10, 11 AS c11, 12 AS c12,
                'complete column' AS c13
            FROM range(57) AS t(i)"""
        )
        assert "9223372036854775807" in last_tool_text(client)
        client.expect(
            "exact batches retain the preview, lookahead, and remaining rows\n",
            # fmt: r
            r=code("""
                result <- .console$sql_last_result()
                stopifnot(identical(result, .console$sql_last_result()))
                first <- result$fetch(5L)
                second <- result$fetch(19L)
                third <- result$fetch(33L)
                empty <- result$fetch(1L)
                stopifnot(inherits(first, "nanoarrow_array_stream"))
                a <- nanoarrow::collect_array_stream(first)
                b <- nanoarrow::collect_array_stream(second)
                c <- nanoarrow::collect_array_stream(third)
                batches <- c(a, b, c)
                stopifnot(length(nanoarrow::collect_array_stream(empty)) == 0L)
                stopifnot(length(result$columns) == 13L)
                ids <- unlist(lapply(batches, function(x) as.vector(x$children$id)))
                stopifnot(identical(ids, 0:56))
                schema <- nanoarrow::infer_nanoarrow_schema(a[[1L]])
                stopifnot(grepl("int64", format(schema$children$big)))
                stopifnot(grepl("decimal128(38, 9)", format(schema$children$exact), fixed = TRUE))
                # Compare exact native buffers from every chunk with their original
                # preview row, without a lossy data-frame conversion or SQL replay.
                big <- as.raw(c(rep(255L, 7L), 127L))
                exact <- as.raw(c(21L, 95L, 216L, 228L, 80L, 144L, 73L, 196L,
                  19L, 51L, 2L, 240L, 246L, 176L, 73L, 9L))
                for (batch in batches) {
                  stopifnot(length(batch$children) == 13L)
                  stopifnot(all(as.vector(batch$children$missing) |> is.na()))
                  stopifnot(all(nchar(as.vector(batch$children$long_value)) == 1000L))
                  stopifnot(all(as.vector(batch$children$c13) == "complete column"))
                  for (field in c("big", "exact")) {
                    column <- batch$children[[field]]
                    expected <- get(field)
                    bytes <- as.raw(column$buffers[[2L]])
                    start <- column$offset * length(expected)
                    stopifnot(identical(
                      bytes[seq.int(start + 1L, start + column$length * length(expected))],
                      rep(expected, column$length)
                    ))
                  }
                }
                sequences <- unlist(lapply(batches, function(x) as.vector(x$children$execution)))
                stopifnot(identical(as.numeric(sequences), as.numeric(1:57)))
                stopifnot(metrics$executions == 1L)
                stopifnot(identical(metrics$fetches, c(21, 3, 33, 1)))
                suppressMessages(invisible(untrace("dbSendQueryArrow", where = asNamespace("DBI"))))
                suppressMessages(invisible(untrace("dbFetchArrowChunk", where = asNamespace("DBI"))))
                stopifnot(DBI::dbGetQuery(.console$sql_connection(), "SELECT currval('executions') AS n")$n == 57)
                writeLines("exact batches retain the preview, lookahead, and remaining rows")
                """),
        )
        client.send(sql="SELECT NULL::DECIMAL(10,2) AS amount WHERE FALSE")
        client.expect(
            "empty stream keeps its exact schema\n",
            # fmt: r
            r=code("""
                empty <- .console$sql_last_result()$fetch()
                stopifnot(grepl(
                  "decimal128(10, 2)",
                  format(empty$get_schema()$children$amount),
                  fixed = TRUE
                ))
                stopifnot(is.null(empty$get_next()))
                writeLines("empty stream keeps its exact schema")
                """),
        )
        client.send(
            sql="""SET TimeZone = 'America/New_York';
            SELECT TIMESTAMPTZ '2026-10-18 12:00:00+00' AS zoned FROM range(30)"""
        )
        assert "2026-10-18 08:00:00-04" in last_tool_text(client)
        client.expect(
            "timestamp preview and retained schema keep the connection timezone\n",
            # fmt: r
            r=code("""
                result <- .console$sql_last_result()
                chunk <- result$fetch(5L)
                stopifnot(grepl("America/New_York", chunk$get_schema()$children$zoned$format))
                result$close()
                writeLines("timestamp preview and retained schema keep the connection timezone")
                """),
        )
        return client.finish()


@requires(R, SQL)
@executions(RUNTIME)
def test_dbi_bounds_fetches_and_preserves_transactions(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.expect(
            requirements={"r": ["RSQLite"]},
            # fmt: r
            r=code("""
                library(DBI)
                metrics <- new.env()
                metrics$executions <- 0L
                metrics$fetches <- numeric()
                metrics$clears <- 0L
                metrics$gate <- FALSE
                metrics$fail <- FALSE
                setClass(
                  "CountConnection",
                  contains = "DBIConnection",
                  slots = c(native = "ANY")
                )
                setClass("CountResult", contains = "DBIResult", slots = c(native = "ANY"))
                setMethod(
                  "dbSendQuery",
                  c("CountConnection", "character"),
                  function(conn, statement, ...) {
                    metrics$executions <- metrics$executions + 1L
                    new("CountResult", native = DBI::dbSendQuery(conn@native, statement, ...))
                  }
                )
                setMethod("dbFetch", "CountResult", function(res, n = -1, ...) {
                  metrics$fetches <- c(metrics$fetches, n)
                  if (metrics$fail) {
                    stop("native fetch failed")
                  }
                  if (metrics$gate) {
                    metrics$gate <- FALSE
                    readline("fetch> ")
                  }
                  DBI::dbFetch(res@native, n = n, ...)
                })
                setMethod("dbColumnInfo", "CountResult", function(res, ...) {
                  DBI::dbColumnInfo(res@native)
                })
                setMethod("dbIsValid", "CountResult", function(dbObj, ...) {
                  DBI::dbIsValid(dbObj@native)
                })
                setMethod("dbIsValid", "CountConnection", function(dbObj, ...) {
                  DBI::dbIsValid(dbObj@native)
                })
                setMethod("dbHasCompleted", "CountResult", function(res, ...) {
                  DBI::dbHasCompleted(res@native)
                })
                setMethod("dbClearResult", "CountResult", function(res, ...) {
                  metrics$clears <- metrics$clears + 1L
                  DBI::dbClearResult(res@native)
                })
                sqlite <- DBI::dbConnect(RSQLite::SQLite(), ":memory:")
                invisible(DBI::dbExecute(sqlite, "CREATE TABLE transaction_state (id INTEGER)"))
                DBI::dbBegin(sqlite)
                invisible(DBI::dbExecute(sqlite, "INSERT INTO transaction_state VALUES (1)"))
                .console$sql_connection(new("CountConnection", native = sqlite))
                """),
        )
        client.send(
            sql="""WITH RECURSIVE rows(i) AS (
            VALUES (0) UNION ALL SELECT i + 1 FROM rows WHERE i < 56
        ) SELECT i, NULL AS missing, 'complete' AS label FROM rows"""
        )
        client.expect(
            "DBI fetch bounds and native transaction retained\n",
            # fmt: r
            r=code("""
                result <- .console$sql_last_result()
                stopifnot(identical(metrics$fetches, 21), metrics$clears == 0L)
                stopifnot(nrow(result$fetch(0L)) == 0L)
                a <- result$fetch(5L)
                stopifnot(identical(metrics$fetches, 21))
                b <- result$fetch(19L)
                stopifnot(identical(metrics$fetches, c(21, 3)))
                c <- result$fetch()
                rows <- rbind(a, b, c)
                stopifnot(identical(rows$i, 0:56), all(is.na(rows$missing)))
                stopifnot(identical(result$columns, names(rows)))
                stopifnot(metrics$executions == 1L, metrics$clears == 1L)
                stopifnot(nrow(result$fetch()) == 0L)
                DBI::dbRollback(sqlite)
                stopifnot(DBI::dbGetQuery(sqlite, "SELECT count(*) AS n FROM transaction_state")$n == 0L)
                writeLines("DBI fetch bounds and native transaction retained")
                """),
        )
        client.send(sql="SELECT 1 AS id WHERE FALSE")
        client.expect(
            "empty DBI frame preserves columns\n",
            # fmt: r
            r=code("""
                result <- .console$sql_last_result()
                stopifnot(identical(result$columns, "id"), nrow(result$fetch()) == 0L)
                writeLines("empty DBI frame preserves columns")
                """),
        )
        query = """WITH RECURSIVE rows(i) AS (
            VALUES (0) UNION ALL SELECT i + 1 FROM rows WHERE i < 56
        ) SELECT i FROM rows"""
        client.send(sql=query)
        client.expect(r="result <- .console$sql_last_result(); metrics$gate <- TRUE")
        client.send(r="result$fetch(30L)", timeout_ms=0)
        wait_for_evaluation_output(
            client,
            '[input requested: "fetch> "]\n[waiting for stdin]',
            "DBI retrieval reached its interrupt gate",
        )
        client.expect("\n", control="interrupt")
        client.expect(
            "interrupted DBI fetch closes its result\n",
            # fmt: r
            r=code("""
                stopifnot(grepl("closed", tryCatch(result$fetch(1L), error = conditionMessage)))
                stopifnot(metrics$clears == 3L, DBI::dbIsValid(sqlite))
                writeLines("interrupted DBI fetch closes its result")
                """),
        )
        client.send(sql=query)
        client.expect(
            "failed DBI fetch closes its result\n",
            # fmt: r
            r=code("""
                result <- .console$sql_last_result()
                metrics$fail <- TRUE
                message <- tryCatch(result$fetch(30L), error = conditionMessage)
                stopifnot(identical(message, "native fetch failed"), metrics$clears == 4L)
                metrics$fail <- FALSE
                .console$sql_connection(NULL)
                DBI::dbDisconnect(sqlite)
                writeLines("failed DBI fetch closes its result")
                """),
        )
        return client.finish()


@requires(R, SQL)
@executions(RUNTIME)
def test_invalidates_closes_and_restarts(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.expect(
            # fmt: python
            python=code("""
                import sqlite3

                connection = sqlite3.connect(":memory:")
                connection.execute("CREATE TABLE values_table (id INTEGER)")
                connection.execute("INSERT INTO values_table VALUES (42)")
                _console.sql_connection(connection)
                """),
        )
        query = """WITH RECURSIVE rows(i) AS (
            VALUES (0) UNION ALL SELECT i + 1 FROM rows WHERE i < 56
        ) SELECT i FROM rows"""
        client.send(sql=query)
        client.expect(
            "explicit close is idempotent\n",
            # fmt: python
            python=code("""
                closed = _console.sql_last_result()
                closed.close()
                closed.close()
                try:
                    closed.fetch()
                except RuntimeError as error:
                    assert "closed" in str(error)
                else:
                    raise AssertionError("closed result fetched rows")
                assert connection.in_transaction
                print("explicit close is idempotent")
                """),
        )
        client.send(sql=query)
        client.expect(python="previous = _console.sql_last_result()")
        client.send(sql="SELECT * FROM missing_table")
        assert "missing_table" in last_tool_text(client)
        client.expect(
            "failed SQL invalidates the previous result\n",
            # fmt: python
            python=code("""
                for fetch in (previous.fetch, _console.sql_last_result):
                    try:
                        fetch()
                    except RuntimeError:
                        pass
                    else:
                        raise AssertionError("stale result survived failed SQL")
                assert connection.in_transaction
                print("failed SQL invalidates the previous result")
                """),
        )
        client.send(sql=query)
        client.expect(python="previous = _console.sql_last_result()")
        client.expect(r=".console$sql_connection(NULL)")
        client.expect(
            "R selection invalidates Python results without committing\n",
            # fmt: python
            python=code("""
                try:
                    previous.fetch(1)
                except RuntimeError as error:
                    assert "invalidated" in str(error)
                else:
                    raise AssertionError("stale Python result")
                assert connection.in_transaction
                connection.rollback()
                assert connection.execute("SELECT count(*) FROM values_table").fetchone() == (0,)
                print("R selection invalidates Python results without committing")
                """),
        )
        client.send(sql="SELECT i::INTEGER AS id FROM range(57) AS t(i)")
        client.expect(r="previous <- .console$sql_last_result()")
        client.expect(python="_console.sql_connection(connection)")
        client.expect(
            "Python selection invalidates R results\n",
            # fmt: r
            r=code("""
                message <- tryCatch(previous$fetch(1L), error = conditionMessage)
                stopifnot(grepl("invalidated", message))
                writeLines("Python selection invalidates R results")
                """),
        )
        client.expect(
            # fmt: python
            python=code("""
                def select_again():
                    _console.sql_connection(connection)
                    return 41


                connection.create_function("select_again", 0, select_again)
                """),
        )
        client.send(sql="SELECT select_again() AS value")
        client.expect(
            "selection during execution does not retain a stale result\n",
            # fmt: python
            python=code("""
                try:
                    _console.sql_last_result()
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("selection during SQL retained a stale result")
                print("selection during execution does not retain a stale result")
                """),
        )
        client.expect(
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[done]",
            control="restart",
            python="assert 'previous' not in globals()",
        )
        client.expect(
            "restart has no retained result\n",
            # fmt: python
            python=code("""
                try:
                    _console.sql_last_result()
                except RuntimeError as error:
                    assert "No retained" in str(error)
                else:
                    raise AssertionError("result survived restart")
                print("restart has no retained result")
                """),
        )
        return client.finish()


@requires(SQL)
@executions(RUNTIME)
def test_slow_fetch_polls_interrupts_and_releases_cursors(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.expect(
            # fmt: python
            python=code("""
                import sqlite3


                class GatedCursor:
                    def __init__(self):
                        self.native = connection.native.cursor()
                        self.calls = 0
                        self.closed = False
                        connection.latest = self

                    def execute(self, source):
                        self.native.execute(source)
                        return self

                    @property
                    def description(self):
                        return self.native.description

                    def fetchmany(self, n):
                        self.calls += 1
                        if self.calls > 1:
                            input("fetch> ")
                        return self.native.fetchmany(n)

                    def close(self):
                        self.closed = True
                        self.native.close()


                class GatedConnection:
                    def __init__(self):
                        self.native = sqlite3.connect(":memory:")

                    def cursor(self):
                        return GatedCursor()


                connection = GatedConnection()
                _console.sql_connection(connection)
                """),
        )
        query = """WITH RECURSIVE rows(i) AS (
            VALUES (0) UNION ALL SELECT i + 1 FROM rows WHERE i < 56
        ) SELECT i FROM rows"""
        client.send(sql=query)
        client.expect(python="result = _console.sql_last_result()")
        client.send(python="rows = result.fetch(30)", timeout_ms=0)
        wait_for_evaluation_output(
            client,
            '[input requested: "fetch> "]\n[waiting for stdin]',
            "retrieval entered its native fetch gate",
        )
        client.expect(stdin="continue\n")
        client.expect(
            "resumed retrieval keeps the preview prefix\n",
            # fmt: python
            python=code("""
                assert rows == [(i,) for i in range(30)]
                assert connection.latest.calls == 2
                result.close()
                assert connection.latest.closed
                print("resumed retrieval keeps the preview prefix")
                """),
        )
        client.send(sql=query)
        client.expect(python="result = _console.sql_last_result()")
        client.send(python="result.fetch(30)", timeout_ms=0)
        wait_for_evaluation_output(
            client,
            '[input requested: "fetch> "]\n[waiting for stdin]',
            "retrieval entered its interrupt gate",
        )
        client.send(control="interrupt")
        assert "KeyboardInterrupt" in last_tool_text(client)
        client.expect(
            "interrupted retrieval releases the cursor\n",
            # fmt: python
            python=code("""
                assert connection.latest.closed
                try:
                    result.fetch(1)
                except RuntimeError as error:
                    assert "closed" in str(error)
                else:
                    raise AssertionError("interrupted cursor remained usable")
                print("interrupted retrieval releases the cursor")
                """),
        )
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
