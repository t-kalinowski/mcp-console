#!/usr/bin/env -S uv run --script

import errno
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import (
    McpClient,
    Transcript,
    code,
    r_test_environment,
    run_this_suite,
    stop_client,
    wait_for_worker_file,
)

PLATFORMS = {"darwin", "linux"}


def test_uses_default_duckdb_extensions(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=workspace,
        )
        client._initialize_and_list_tools()

        sql = code(r"""
            SELECT
              CASE
                WHEN json_extract_string('{"answer": 42}', '$.answer') = '42'
                THEN 1234567
                ELSE 0
              END AS json_ok,
              CASE
                WHEN timezone('America/New_York', TIMESTAMP '2020-01-01') IS NOT NULL
                THEN 1234567
                ELSE 0
              END AS icu_ok1
            """)
        client.send(sql=sql)
        preview = last_tool_text(client)
        assert preview.splitlines()[-1].split() == ["1", "1234567", "1234567"]

        sql = code(r"""
            SELECT CASE WHEN count(*) = 2 THEN 1234567 ELSE 0 END AS loaded1
            FROM duckdb_extensions()
            WHERE extension_name IN ('icu', 'json') AND loaded
            """)
        client.send(sql=sql)
        preview = last_tool_text(client)
        assert preview.splitlines()[-1].split() == ["1", "1234567"]
        return client._finish()


def test_restart_adds_r_and_duckdb_requirements(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        ambient_library = workspace / "ambient-library"
        ambient_library.mkdir()
        environment["R_LIBS"] = str(ambient_library)
        environment["R_LIBS_SITE"] = str(ambient_library)
        environment["R_LIBS_USER"] = str(ambient_library)
        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=workspace,
        )
        client._initialize_and_list_tools()
        client.send(r="restart_marker <- 42L")
        assert last_tool_text(client) == "[done]"

        client.send(
            control="restart",
            requirements={
                "r": ["praise"],
                "duckdb": ["not_a_real_duckdb_extension"],
            },
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        failure = result["content"][0]["text"]
        assert (
            'Failed to download extension "not_a_real_duckdb_extension"' in failure
        ), failure
        result["content"][0]["text"] = duckdb_native_failure(failure)

        client.send(r="identical(restart_marker, 42L)")
        assert last_tool_text(client) == "[1] TRUE\n"

        client.send(
            control="restart",
            requirements={"r": ["praise"], "duckdb": ["fts"]},
        )
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )

        client.send(
            r=(
                "!exists('restart_marker') && "
                "requireNamespace('praise', quietly = TRUE)"
            )
        )
        assert last_tool_text(client) == "[1] TRUE\n"

        sql = code(r"""
            CREATE TABLE restart_documents AS
            SELECT 1 AS id, 'duckdb restart requirements' AS body
            """)
        client.send(sql=sql)
        assert last_tool_text(client) == "[done]"
        client.send(sql="PRAGMA create_fts_index('restart_documents', 'id', 'body')")
        assert last_tool_text(client) == "[done]"
        sql = code(r"""
            SELECT count(*) AS matches
            FROM restart_documents
            WHERE fts_main_restart_documents.match_bm25(id, 'duckdb') IS NOT NULL
            """)
        client.send(sql=sql)
        preview = last_tool_text(client)
        assert preview.splitlines()[-1].split() == ["1", "1"]
        return client._finish()


def test_prepares_and_loads_duckdb_extensions(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=workspace,
        )
        client._initialize_and_list_tools()

        sql = code(r"""
            CREATE TABLE retained_state AS
            SELECT
              CAST(42 AS INTEGER) AS answer,
              'duckdb extension preparation' AS body
            UNION ALL
            SELECT 7, 'other words'
            """)
        client.send(sql=sql)
        assert last_tool_text(client) == "[done]"

        client.send(
            requirements={"duckdb": ["json"]},
        )
        assert last_tool_text(client) == "[prepared]"

        client.send(
            requirements={"r": ["praise"]},
        )
        assert last_tool_text(client) == "[prepared]"

        client.send(
            sql="CREATE TABLE failed_requirement_cell(answer INTEGER)",
            requirements={"duckdb": ["not_a_real_duckdb_extension"]},
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        failure = result["content"][0]["text"]
        assert failure.startswith("DuckDB extension resolution failed with "), failure
        assert (
            'Failed to download extension "not_a_real_duckdb_extension"' in failure
        ), failure
        assert "unknown core DuckDB extension" not in failure, failure
        result["content"][0]["text"] = duckdb_native_failure(failure)

        client.send(
            sql=(
                "SELECT count(*) AS side_effects FROM duckdb_tables() "
                "WHERE table_name = 'failed_requirement_cell'"
            )
        )
        assert last_tool_text(client).splitlines()[-1].split() == ["1", "0"]

        sql = code(r"""
            PRAGMA create_fts_index('retained_state', 'answer', 'body')
            """)
        client.send(sql=sql, requirements={"duckdb": ["fts"]})
        assert last_tool_text(client) == "[done]"

        client.send(
            requirements={"duckdb": ["fts"]},
        )
        assert last_tool_text(client) == "[prepared]"

        sql = code(r"""
            SELECT
              'loaded' AS verified
            FROM retained_state
            CROSS JOIN duckdb_extensions() AS extensions
            WHERE retained_state.answer = 42
              AND extensions.extension_name = 'fts'
              AND extensions.loaded
              AND fts_main_retained_state.match_bm25(
                retained_state.answer,
                'duckdb'
              ) IS NOT NULL
            """)
        client.send(sql=sql)
        preview = last_tool_text(client)
        assert preview.splitlines()[-1].split() == ["1", '"loaded"']

        client.send(control="restart")
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )

        client.send(sql="LOAD fts")
        assert last_tool_text(client) == "[done]"
        sql = code(r"""
            CREATE TABLE replacement_state AS
            SELECT
              CAST(42 AS INTEGER) AS answer,
              'duckdb extension preparation' AS body
            """)
        client.send(sql=sql)
        assert last_tool_text(client) == "[done]"
        sql = code(r"""
            PRAGMA create_fts_index('replacement_state', 'answer', 'body')
            """)
        client.send(sql=sql)
        assert last_tool_text(client) == "[done]"
        sql = code(r"""
            SELECT 'loaded' AS verified
            FROM replacement_state
            CROSS JOIN duckdb_extensions() AS extensions
            WHERE extensions.extension_name = 'fts'
              AND extensions.loaded
              AND fts_main_replacement_state.match_bm25(
                replacement_state.answer,
                'duckdb'
              ) IS NOT NULL
            """)
        client.send(sql=sql)
        preview = last_tool_text(client)
        assert preview.splitlines()[-1].split() == ["1", '"loaded"']
        return client._finish()


def test_sends_sql_cell_with_initial_requirements(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()

    sql = code(r"""
        LOAD fts;
        SELECT count(*) AS loaded
        FROM duckdb_extensions()
        WHERE extension_name = 'fts' AND loaded
        """)
    client.send(sql=sql, requirements={"duckdb": ["fts"]})
    assert last_tool_text(client).splitlines()[-1].split() == ["1", "1"]
    return client._finish()


def test_queries_a_ragnar_store_created_in_r(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    temporary = tempfile.TemporaryDirectory()
    workspace = Path(temporary.name)
    client = McpClient(
        binary,
        ("serve",),
        environment,
        current_directory=workspace,
    )
    client._initialize_and_list_tools()

    sql = code(r"""
        CREATE TEMP TABLE before_prepare AS
        SELECT CAST(42 AS INTEGER) AS value
        """)
    client.send(sql=sql)
    assert last_tool_text(client) == "[done]"

    client.send(
        requirements={"r": ["ragnar"], "duckdb": ["fts", "vss"]},
    )
    assert last_tool_text(client) == "[prepared]"

    # fmt: r
    r = code(r"""
        stopifnot(
          identical(
            DBI::dbGetQuery(
              sql_connection(),
              "SELECT value FROM before_prepare"
            )$value,
            42L
          ),
          identical(dirname(find.package("ragnar")), .libPaths()[[1L]])
        )
        embed_banana <- function(x) {
          out <- matrix(1, nrow = length(x), ncol = 2L)
          out[, 1L] <- grepl("banana", x, ignore.case = TRUE)
          out
        }
        store_path <- file.path(tempdir(), "knowledge.ragnar.duckdb")
        store <- suppressMessages(ragnar::ragnar_store_create(
          location = store_path,
          embed = embed_banana,
          embedding_size = 2L
        ))
        documents <- list(
          ragnar::MarkdownDocument(
            "# Alpha\n\nApples are red fruit.",
            origin = "alpha.md"
          ),
          ragnar::MarkdownDocument(
            "# Beta\n\nBananas are yellow fruit.",
            origin = "beta.md"
          )
        )
        for (document in documents) {
          chunks <- ragnar::markdown_chunk(
            document,
            target_size = 1000L,
            target_overlap = 0
          )
          ragnar::ragnar_store_insert(store, chunks)
        }
        ragnar::ragnar_store_build_index(store, type = c("vss", "fts"))
        connection <- sql_connection()
        invisible(DBI::dbExecute(
          connection,
          paste(
            "ATTACH",
            DBI::dbQuoteString(connection, store_path),
            "AS knowledge (READ_ONLY)"
          )
        ))
        invisible(DBI::dbExecute(connection, "USE knowledge"))
        writeLines("ragnar store ready")
        """)
    client.send(r=r)
    marker = "ragnar store ready\n"
    assert normalize_duckdb_progress(client) == marker

    sql = code(r"""
        LOAD fts;
        LOAD vss;
        SELECT
          document.origin AS vss_origin,
          nearest.distance,
          (
            SELECT origin
            FROM chunks
            WHERE fts_main_chunks.match_bm25(chunk_id, 'bananas') IS NOT NULL
            ORDER BY origin
            LIMIT 1
          ) AS fts_origin,
          (SELECT value FROM before_prepare) AS retained
        FROM (
          SELECT
            doc_id,
            array_cosine_distance(
              embedding,
              [1, 1]::FLOAT[2]
            ) AS distance
          FROM embeddings
          ORDER BY distance
          LIMIT 1
        ) AS nearest
        JOIN documents AS document USING (doc_id)
        ORDER BY nearest.distance
        """)
    client.send(sql=sql)
    preview = last_tool_text(client)
    assert preview.splitlines()[-1].split() == [
        "1",
        '"beta.md"',
        "0.0",
        '"beta.md"',
        "42",
    ]
    assert '"alpha.md"' not in preview
    transcript = client._finish()
    temporary.cleanup()
    return transcript


def test_uses_ragnar_like_the_guide_and_adapts_to_the_console(
    binary: Path,
) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    temporary = tempfile.TemporaryDirectory()
    workspace = Path(temporary.name)
    client = McpClient(
        binary,
        ("serve",),
        environment,
        current_directory=workspace,
    )
    client._initialize_and_list_tools()

    sql = code(r"""
        CREATE TABLE agent_notes AS
        SELECT 'worker catalog' AS note
        """)
    client.send(sql=sql)
    assert last_tool_text(client) == "[done]"

    client.send(requirements={"r": ["ragnar"]})
    assert last_tool_text(client) == "[prepared]"

    r = code(r"""
        ragnar::ragnar_store_create(
          "knowledge.ragnar.duckdb",
          embed = NULL
        )
        """)
    client.send(r=r)
    output = normalize_duckdb_progress(client)
    assert "knowledge.ragnar.duckdb" in output
    denial_messages = {
        os.strerror(errno.EACCES),
        os.strerror(errno.EPERM),
        os.strerror(errno.EROFS),
    }
    observed_denials = [message for message in denial_messages if message in output]
    assert len(observed_denials) == 1, output
    output = output.replace(observed_denials[0], os.strerror(errno.EPERM))
    for directory in (str(workspace.resolve()), str(workspace)):
        output = output.replace(directory, "<workspace>")
    client.transcript[-1]["result"]["content"][0]["text"] = output
    assert not (workspace / "knowledge.ragnar.duckdb").exists()

    # fmt: r
    r = code(r"""
        store_path <- file.path(tempdir(), "knowledge.ragnar.duckdb")
        store <- suppressMessages(ragnar::ragnar_store_create(
          store_path,
          embed = NULL
        ))
        documents <- list(
          ragnar::MarkdownDocument(
            "# Alpha\n\nApples are red fruit.",
            origin = "alpha.md"
          ),
          ragnar::MarkdownDocument(
            "# Beta\n\nBananas are yellow fruit.",
            origin = "beta.md"
          )
        )
        for (document in documents) {
          chunks <- ragnar::markdown_chunk(
            document,
            target_size = 1000L,
            target_overlap = 0
          )
          ragnar::ragnar_store_insert(store, chunks)
        }
        writeLines("created store under the worker tempdir")
        """)
    client.send(r=r)
    assert normalize_duckdb_progress(client) == (
        "created store under the worker tempdir\n"
    )

    client.send(
        requirements={"duckdb": ["fts", "vss"]},
    )
    assert last_tool_text(client) == "[prepared]"

    r = code(r"""
        stopifnot(
          DBI::dbIsValid(store@con),
          DBI::dbGetQuery(store@con, "SELECT count(*) AS n FROM chunks")$n == 2
        )
        ragnar::ragnar_store_build_index(store)
        writeLines("index built after extension preparation")
        """)
    client.send(r=r)
    assert normalize_duckdb_progress(client) == (
        "index built after extension preparation\n"
    )

    r = code(r"""
        creator_result <- ragnar::ragnar_retrieve(
          store,
          "bananas",
          top_k = 1L
        )
        creator_result[c("origin", "text")]
        """)
    client.send(r=r)
    preview = normalize_duckdb_progress(client)
    assert "beta.md" in preview and "Bananas are yellow fruit" in preview
    assert "alpha.md" not in preview

    r = code(r"""
        reader <- ragnar::ragnar_store_connect(
          store_path,
          read_only = TRUE
        )
        reader_result <- ragnar::ragnar_retrieve(
          reader,
          "apples",
          top_k = 1L
        )
        reader_result[c("origin", "text")]
        """)
    client.send(r=r)
    preview = normalize_duckdb_progress(client)
    assert "alpha.md" in preview and "Apples are red fruit" in preview
    assert "beta.md" not in preview

    sql = code(r"""
        SELECT origin FROM chunks ORDER BY origin
        """)
    client.send(sql=sql)
    output = normalize_trailing_spaces(client)
    assert "Binder Error:" in output
    assert 'Referenced column "origin" not found' in output

    r = code(r"""
        writeLines(paste(
          "R chunks columns:",
          paste(names(chunks), collapse = ", ")
        ))
        rm(chunks)
        """)
    client.send(r=r)
    assert last_tool_text(client) == ("R chunks columns: start, end, context, text\n")

    client.send(sql=sql)
    output = normalize_trailing_spaces(client)
    assert "Catalog Error:" in output
    assert "Table with name chunks does not exist" in output

    r = code(r"""
        sql_connection(reader@con)
        """)
    client.send(r=r)
    assert last_tool_text(client) == (
        "Error in sql_connection(reader@con) : unused argument (reader@con)\n"
    )

    r = code(r"""
        connection <- sql_connection()
        stopifnot(
          DBI::dbIsValid(store@con),
          DBI::dbIsValid(reader@con),
          !identical(connection, store@con),
          !identical(connection, reader@con)
        )
        invisible(DBI::dbExecute(
          connection,
          paste(
            "ATTACH",
            DBI::dbQuoteString(connection, store_path),
            "AS knowledge (READ_ONLY)"
          )
        ))
        writeLines("attached with both ragnar connections still open")
        """)
    client.send(r=r)
    assert last_tool_text(client) == (
        "attached with both ragnar connections still open\n"
    )

    sql = code(r"""
        SELECT origin FROM knowledge.main.chunks ORDER BY origin
        """)
    client.send(sql=sql)
    preview = normalize_trailing_spaces(client)
    assert [line.split() for line in preview.splitlines()[-2:]] == [
        ["1", '"alpha.md"'],
        ["2", '"beta.md"'],
    ]

    sql = code(r"""
        SELECT origin
        FROM knowledge.main.chunks
        WHERE knowledge.fts_main_chunks.match_bm25(
          chunk_id,
          'bananas'
        ) IS NOT NULL
        ORDER BY origin
        """)
    client.send(sql=sql)
    output = normalize_trailing_spaces(client)
    assert 'Table with name "fts_main_chunks.terms" does not exist' in output
    assert 'schema "fts_main_chunks" does not exist' in output

    sql = code(r"""
        USE knowledge;
        SELECT
          origin,
          (SELECT note FROM memory.main.agent_notes) AS note
        FROM chunks
        WHERE fts_main_chunks.match_bm25(chunk_id, 'bananas') IS NOT NULL
        ORDER BY origin
        """)
    client.send(sql=sql)
    preview = normalize_trailing_spaces(client)
    assert preview.splitlines()[-1].split() == [
        "1",
        '"beta.md"',
        '"worker',
        'catalog"',
    ]

    transcript = client._finish()
    temporary.cleanup()
    return transcript


def test_evaluates_queries_in_a_persistent_catalog(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        ambient_library = workspace / "ambient-library"
        ambient_library.mkdir()
        environment = os.environ.copy()
        environment["R_LIBS"] = str(ambient_library)
        environment["R_LIBS_SITE"] = str(ambient_library)
        environment["R_LIBS_USER"] = str(ambient_library)
        environment["RETICULATE_PYTHON"] = ""
        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=workspace,
        )
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


def test_interrupts_running_sql_query(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment, _ = r_test_environment()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=temporary_path,
        )
        passed = False
        try:
            client._initialize_and_list_tools()
            # fmt: r
            r = code(r"""
                invisible(DBI::dbExecute(
                  sql_connection(),
                  "SET VARIABLE sql_interrupt_marker = ?",
                  params = list(file.path(tempdir(), "sql-interrupt-started"))
                ))
                """)
            client.send(r=r)
            output = last_tool_text(client)
            assert output == "[done]", repr(output)

            sql = code(r"""
                CREATE TABLE interrupt_state AS
                SELECT CAST(42 AS INTEGER) AS answer
                """)
            client.send(sql=sql)
            assert last_tool_text(client) == "[done]"

            sql = code(r"""
                COPY (SELECT 1) TO (getvariable('sql_interrupt_marker'));
                SELECT sleep_ms(60000) AS waited
                """)
            client.send(sql=sql, timeout_ms=0)
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            wait_for_worker_file(
                temporary_path,
                "sql-interrupt-started",
                client,
            )
            result = client.send(
                control="interrupt",
                timeout_ms=30_000,
            )
            assert result["isError"] is False, result
            output = last_tool_text(client)
            assert output in {"\n", "\n\n"}, repr(output)
            # DuckDB and R can each publish the native interrupt newline.
            result["content"][0]["text"] = "\n"

            client.send(sql="SELECT answer FROM interrupt_state")
            assert "42" in last_tool_text(client)
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_client(client)


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


def test_uses_200_column_default(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    sql = code(r"""
        SELECT
          1 AS column_name_01_abcdefghijkl,
          2 AS column_name_02_abcdefghijkl,
          3 AS column_name_03_abcdefghijkl,
          4 AS column_name_04_abcdefghijkl,
          5 AS column_name_05_abcdefghijkl,
          6 AS column_name_06_abcdefghijkl
        """)
    client.send(sql=sql)
    output = last_tool_text(client)
    for column in range(1, 7):
        assert f"column_name_{column:02}_abcdefghijkl" in output
    assert "abbreviated name" not in output
    header = next(
        line for line in output.splitlines() if line.startswith("  column_name_01")
    )
    assert 160 < len(header) <= 200, repr(header)
    return client._finish()


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
        SELECT repeat('z', 1000) AS value
        """)
    client.send(sql=sql)
    long_cell = last_tool_text(client)

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
    assert f'"{"z" * 159}…"' in long_cell
    assert "[cell values truncated to 160 characters]" in long_cell
    assert large != "\n[running; poll with an empty send]"
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


def normalize_duckdb_progress(client: McpClient) -> str:
    output = last_tool_text(client)
    sections = output.split("\r")
    assert all(
        not section.strip() or section.startswith("DuckDB progress:")
        for section in sections[:-1]
    ), output
    output = sections[-1]
    client.transcript[-1]["result"]["content"][0]["text"] = output
    return normalize_trailing_spaces(client)


def normalize_duckdb_extension_error(client: McpClient) -> str:
    output = normalize_duckdb_progress(client)
    output, download_urls = re.subn(
        r'(?<= at URL )"https?://[^"]+"',
        '"<DuckDB extension URL>"',
        output,
        count=1,
    )
    output, troubleshooting_urls = re.subn(
        r"https://duckdb\.org/docs/stable/extensions/troubleshooting\?\S+",
        "<DuckDB extension troubleshooting URL>",
        output,
        count=1,
    )
    assert (download_urls, troubleshooting_urls) == (1, 1), output
    client.transcript[-1]["result"]["content"][0]["text"] = output
    return output


def normalize_trailing_spaces(client: McpClient) -> str:
    output = last_tool_text(client)
    trailing_newline = output.endswith("\n")
    output = "\n".join(line.rstrip() for line in output.splitlines())
    if trailing_newline:
        output += "\n"
    client.transcript[-1]["result"]["content"][0]["text"] = output
    return output


def duckdb_native_failure(failure: str) -> str:
    native_failure = next(
        line.strip().removeprefix("! ")
        for line in failure.splitlines()
        if "Failed to download extension" in line
    )
    return native_failure.partition(' at URL "')[0]


if __name__ == "__main__":
    run_this_suite(__file__)
