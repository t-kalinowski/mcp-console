#!/usr/bin/env -S uv run --script

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.assertions import wait_for_evaluation_output
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import R, SQL, requires
from support.suites import run_this_suite


@requires(R, SQL)
@executions(DIRECT, SANDBOXED)
def test_binding_install_syntax_and_batch_errors(
    binary: Path, execution: Execution
) -> Transcript:
    environment, _ = r_test_environment()
    with McpClient(binary, execution.serve(), environment) as client:
        client.initialize_and_list_tools()
        wait_for_evaluation_output(
            client,
            lambda output: output.endswith("R binding contract verified\n"),
            "R binding conformance",
            completion_timeout_seconds=client.response_timeout,
            # fmt: r
            r=code(r"""
                local({
                  connection <- DBI::dbConnect(
                    duckdb::duckdb(),
                    dbdir = ":memory:",
                    config = list(enable_external_access = "false")
                  )
                  on.exit(DBI::dbDisconnect(connection, shutdown = TRUE))
                  syntax_error <- "Parser Error: syntax error at end of input"
                  for (statement in c(
                    "INSTALL json",
                    "INSTALL 'json'",
                    'INSTALL "json"',
                    "INSTALL json FROM core",
                    'INSTALL json FROM "core"',
                    "INSTALL json FROM 'core'"
                  )) {
                    error <- tryCatch(
                      DBI::dbExecute(connection, paste0(statement, "; SELECT (")),
                      error = identity
                    )
                    stopifnot(inherits(error, "error"))
                    stopifnot(identical(
                      strsplit(conditionMessage(error), "\n", fixed = TRUE)[[1]][1],
                      syntax_error
                    ))
                    cat(statement, "\n", conditionMessage(error), "\n", sep = "")
                  }
                  error <- tryCatch(
                    DBI::dbExecute(connection, "CREATE TABLE before_syntax(i INT); SELECT ("),
                    error = identity
                  )
                  stopifnot(
                    inherits(error, "error"),
                    !DBI::dbExistsTable(connection, "before_syntax")
                  )
                  cat(conditionMessage(error), "\n", sep = "")
                  error <- tryCatch(
                    DBI::dbExecute(
                      connection,
                      paste(
                        "CREATE TABLE before_bind(i INT);",
                        "INSERT INTO before_bind VALUES (1);",
                        "SELECT * FROM missing_batch;",
                        "CREATE TABLE after_error(i INT)"
                      )
                    ),
                    error = identity
                  )
                  stopifnot(inherits(error, "error"))
                  stopifnot(identical(
                    DBI::dbGetQuery(connection, "SELECT * FROM before_bind")$i,
                    1L
                  ))
                  stopifnot(!DBI::dbExistsTable(connection, "after_error"))
                  cat(conditionMessage(error), "\n", sep = "")
                  error <- tryCatch(
                    DBI::dbExecute(
                      connection,
                      paste(
                        "BEGIN; CREATE TABLE in_tx(i INT);",
                        "INSERT INTO in_tx VALUES (2);",
                        "SELECT * FROM missing_batch; COMMIT"
                      )
                    ),
                    error = identity
                  )
                  stopifnot(inherits(error, "error"))
                  stopifnot(identical(DBI::dbGetQuery(connection, "SELECT * FROM in_tx")$i, 2L))
                  cat(conditionMessage(error), "\n", sep = "")
                  DBI::dbRollback(connection)
                  stopifnot(!DBI::dbExistsTable(connection, "in_tx"))
                  cat("R binding contract verified\n")
                })
                """),
        )
        wait_for_evaluation_output(
            client,
            lambda output: output.endswith("Python binding contract verified\n"),
            "Python binding conformance",
            completion_timeout_seconds=client.response_timeout,
            requirements={"python": ["duckdb"]},
            # fmt: python
            python=code(r"""
                import duckdb

                for source in [
                    "INSTALL json",
                    "INSTALL 'json'",
                    'INSTALL "json"',
                    "INSTALL json FROM core",
                    'INSTALL json FROM "core"',
                    "INSTALL json FROM 'core'",
                ]:
                    statements = duckdb.extract_statements(source)
                    assert len(statements) == 1
                    assert statements[0].query == source
                    print(source, "parsed")

                with duckdb.connect(
                    ":memory:", config={"enable_external_access": "false"}
                ) as connection:
                    for source, error_type in [
                        ("CREATE TABLE before_syntax(i INT); SELECT (", duckdb.ParserException),
                        (
                            "CREATE TABLE before_bind(i INT); INSERT INTO before_bind VALUES (1); SELECT * FROM missing_batch; CREATE TABLE after_error(i INT)",
                            duckdb.CatalogException,
                        ),
                        (
                            "BEGIN; CREATE TABLE in_tx(i INT); INSERT INTO in_tx VALUES (2); SELECT * FROM missing_batch; COMMIT",
                            duckdb.CatalogException,
                        ),
                    ]:
                        try:
                            connection.execute(source)
                        except error_type as error:
                            print(str(error))
                        else:
                            raise AssertionError("expected batch failure")
                    assert connection.execute("SHOW TABLES").fetchall() == [
                        ("before_bind",),
                        ("in_tx",),
                    ]
                    assert connection.execute("SELECT * FROM before_bind").fetchall() == [(1,)]
                    assert connection.execute("SELECT * FROM in_tx").fetchall() == [(2,)]
                    connection.execute("ROLLBACK")
                    assert connection.execute("SHOW TABLES").fetchall() == [("before_bind",)]
                print("Python binding contract verified")
                """),
        )
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
