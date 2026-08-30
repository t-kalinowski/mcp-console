base::local(
  {
    managed_connection <- NULL
    selected_connection <- NULL
    source <- NULL
    printer_ready <- FALSE
    preview_rows <- 20L
    preview_columns <- 12L
    cell_width <- 160L
    preview_width <- 200L
    response_bytes <- 12L * 1024L

    bridge <- environment()
    send_query_arrow <- eval(
      bquote(
        function() {
          DBI::dbSendQueryArrow(
            .(bridge)$selected_connection,
            .(bridge)$source
          )
        }
      ),
      envir = globalenv()
    )

    ensure_managed_connection <- function() {
      if (!is.null(managed_connection)) {
        return(invisible(managed_connection))
      }

      storage <- file.path(tempdir(), "mcp-console-duckdb")
      managed_connection <<- DBI::dbConnect(
        duckdb::duckdb(
          dbdir = ":memory:",
          config = list(
            # Suppress DuckDB-R's temporary fallback while leaving DuckDB core
            # to resolve its native default extension directory.
            extension_directory = "",
            secret_directory = file.path(storage, "stored-secrets"),
            temp_directory = file.path(storage, "spill")
          ),
          environment_scan = TRUE
        )
      )
      DBI::dbExecute(managed_connection, "SET enable_progress_bar = false")
      invisible(managed_connection)
    }

    ensure_connection <- function() {
      if (is.null(selected_connection)) {
        selected_connection <<- ensure_managed_connection()
      }
      selected_connection
    }

    sql_connection <- function() {
      ensure_connection()
    }

    console_sql_connection <- function(connection) {
      if (is.null(connection)) {
        selected_connection <<- ensure_managed_connection()
        return(invisible(selected_connection))
      }
      if (
        !inherits(connection, "DBIConnection") ||
          !isTRUE(tryCatch(
            DBI::dbIsValid(connection),
            error = function(...) FALSE
          ))
      ) {
        stop("`connection` must be a valid DBIConnection or NULL")
      }
      selected_connection <<- connection
      invisible(selected_connection)
    }
    tools <- base::attach(
      NULL,
      pos = 2L,
      name = "tools:mcp-console",
      warn.conflicts = FALSE
    )
    # Match reticulate's getter-only `py` binding. Attribute assignment such as
    # `py$name <- value` already writes through the returned Python module proxy.
    base::makeActiveBinding("py", function() reticulate::py, tools)
    base::assign("sql_connection", sql_connection, envir = tools)
    base::assign(
      "console_sql_connection",
      console_sql_connection,
      envir = tools
    )

    ensure_printer <- function() {
      if (printer_ready) {
        return(invisible(NULL))
      }

      type_sum <- function(x) {
        attr(x, "arrow_type", exact = TRUE)
      }
      pillar_shaft <- function(x, ...) {
        type <- attr(x, "arrow_type", exact = TRUE)
        quote <- if (grepl("^(large_)?string|^string_view", type)) "\"" else ""
        formatted <- encodeString(
          unclass(x),
          quote = quote,
          na.encode = FALSE
        )
        align <- if (grepl("^(u?int|float|double|decimal)", type)) {
          "right"
        } else {
          "left"
        }
        pillar::new_pillar_shaft_simple(
          formatted,
          align = align,
          min_width = 1L,
          na = "NULL",
          shorten = "back"
        )
      }

      registerS3method(
        "type_sum",
        "mcp_console_sql_column",
        type_sum,
        envir = asNamespace("pillar")
      )
      registerS3method(
        "pillar_shaft",
        "mcp_console_sql_column",
        pillar_shaft,
        envir = asNamespace("pillar")
      )
      printer_ready <<- TRUE
      invisible(NULL)
    }

    arrow_type <- function(schema) {
      sub("^<nanoarrow_schema (.*)>$", "\\1", format(schema))
    }

    fetch_query <- function() {
      result <- send_query_arrow()
      tryCatch(
        {
          if (result@stmt_lst$return_type != "QUERY_RESULT") {
            return(NULL)
          }

          stream <- DBI::dbFetchArrow(
            result,
            chunk_size = preview_rows + 1L
          )
          tryCatch(
            {
              schema <- stream$get_schema()
              batch <- stream$get_next(schema)
              if (!is.null(batch) && batch$length == 0L) {
                batch <- NULL
              }
              list(schema = schema, batch = batch)
            },
            finally = stream$release()
          )
        },
        finally = DBI::dbClearResult(result)
      )
    }

    fetch_dbi <- function() {
      # DBI has no dialect-neutral way to classify a cell before dispatch.
      # Commands that need the statement interface run through DBI from R.
      result <- DBI::dbSendQuery(selected_connection, source)
      tryCatch(
        {
          if (nrow(DBI::dbColumnInfo(result)) == 0L) {
            return(NULL)
          }
          data <- DBI::dbFetch(result, n = preview_rows + 1L)
          schema <- nanoarrow::infer_nanoarrow_schema(data)
          batch <- nanoarrow::as_nanoarrow_array(data, schema = schema)
          if (batch$length == 0L) {
            batch <- NULL
          }
          list(schema = schema, batch = batch)
        },
        finally = DBI::dbClearResult(result)
      )
    }

    stringify <- function(batch, schema, rows, columns, id) {
      render_connection <- ensure_managed_connection()
      array_pointer <- nanoarrow::nanoarrow_allocate_array()
      schema_pointer <- nanoarrow::nanoarrow_allocate_schema()
      nanoarrow::nanoarrow_pointer_export(batch, array_pointer)
      nanoarrow::nanoarrow_pointer_export(schema, schema_pointer)
      table <- arrow::RecordBatch$import_from_c(
        array_pointer,
        schema_pointer
      )
      table <- table$Slice(0L, rows)$SelectColumns(seq_len(columns) - 1L)
      names <- sprintf("column_%02d", seq_len(columns))
      table <- table$RenameColumns(names)
      occupied <- DBI::dbGetQuery(
        render_connection,
        paste(
          "SELECT lower(table_name) AS name",
          "FROM system.information_schema.tables"
        )
      )$name
      occupied <- c(
        occupied,
        tolower(duckdb::duckdb_list_arrow(render_connection))
      )
      relation_base <- paste0("__mcp_console_preview_", id)
      relation <- relation_base
      suffix <- 0L
      while (tolower(relation) %in% occupied) {
        suffix <- suffix + 1L
        relation <- paste0(relation_base, "_", suffix)
      }

      duckdb::duckdb_register_arrow(render_connection, relation, table)
      tryCatch(
        {
          identifiers <- DBI::dbQuoteIdentifier(render_connection, names)
          cast_projection <- paste0(
            "CAST(preview.",
            identifiers,
            " AS VARCHAR) AS ",
            identifiers,
            collapse = ", "
          )
          value_projection <- paste0(
            "CASE WHEN length(strings.",
            identifiers,
            ") > ",
            cell_width,
            " THEN left(strings.",
            identifiers,
            ", ",
            cell_width - 1L,
            ") || '…' ELSE strings.",
            identifiers,
            " END AS ",
            identifiers
          )
          truncated_names <- sprintf("truncated_%02d", seq_len(columns))
          truncated_identifiers <- DBI::dbQuoteIdentifier(
            render_connection,
            truncated_names
          )
          truncated_projection <- paste0(
            "coalesce(length(strings.",
            identifiers,
            ") > ",
            cell_width,
            ", false) AS ",
            truncated_identifiers
          )
          query <- paste(
            "WITH strings AS (SELECT",
            cast_projection,
            "FROM",
            DBI::dbQuoteIdentifier(render_connection, relation),
            "AS preview) SELECT",
            paste(c(value_projection, truncated_projection), collapse = ", "),
            "FROM strings"
          )
          output <- DBI::dbGetQuery(render_connection, query)
          list(
            values = as.list(output[names]),
            truncated = as.list(output[truncated_names])
          )
        },
        finally = duckdb::duckdb_unregister_arrow(
          render_connection,
          relation
        )
      )
    }

    format_table <- function(
      values,
      names,
      types,
      truncated,
      rows,
      columns,
      fetched_rows,
      total_columns,
      more_rows
    ) {
      ensure_printer()
      data <- lapply(seq_len(columns), function(column) {
        structure(
          values[[column]][seq_len(rows)],
          class = c("mcp_console_sql_column", "character"),
          arrow_type = types[[column]]
        )
      })
      names(data) <- names[seq_len(columns)]
      table <- tibble::as_tibble(data, .name_repair = "minimal")

      previous_options <- options(
        OutDec = ".",
        cli.num_colors = 1L,
        cli.unicode = TRUE,
        digits = 7L,
        pillar.advice = FALSE,
        pillar.bidi = FALSE,
        pillar.bold = FALSE,
        pillar.max_extra_cols = columns,
        pillar.max_footer_lines = columns + 1L,
        pillar.min_chars = 3L,
        pillar.min_title_chars = 3L,
        pillar.print_max = preview_rows,
        pillar.subtle = FALSE,
        pillar.superdigit_sep = "\u200b",
        pillar.width = preview_width,
        scipen = 0L,
        width = preview_width
      )
      on.exit(options(previous_options))
      output <- paste(
        format(
          table,
          n = rows,
          width = preview_width,
          max_extra_cols = columns,
          max_footer_lines = columns + 1L
        ),
        collapse = "\n"
      )

      markers <- character()
      if (rows == 0L && fetched_rows == 0L) {
        markers <- c(markers, "[0 rows]")
      }
      if (more_rows || rows < fetched_rows) {
        markers <- c(markers, "[additional rows omitted]")
      }
      omitted_columns <- total_columns - columns
      if (omitted_columns > 0L) {
        suffix <- if (omitted_columns == 1L) "column" else "columns"
        markers <- c(
          markers,
          sprintf("[%d additional %s omitted]", omitted_columns, suffix)
        )
      }
      if (
        any(vapply(
          seq_len(columns),
          function(column) any(truncated[[column]][seq_len(rows)]),
          logical(1)
        ))
      ) {
        markers <- c(markers, "[cell values truncated to 160 characters]")
      }
      paste(c(output, markers), collapse = "\n")
    }

    render_preview <- function(preview, id) {
      schema <- preview$schema
      batch <- preview$batch
      total_columns <- length(schema$children)
      if (total_columns == 0L) {
        return(invisible(NULL))
      }

      columns <- min(total_columns, preview_columns)
      fetched_rows <- if (is.null(batch)) 0L else batch$length
      rows <- min(fetched_rows, preview_rows)
      more_rows <- fetched_rows > preview_rows
      names <- names(schema$children)[seq_len(columns)]
      types <- vapply(
        schema$children[seq_len(columns)],
        arrow_type,
        character(1)
      )

      if (rows == 0L) {
        values <- rep(list(character()), columns)
        truncated <- rep(list(logical()), columns)
      } else {
        cells <- stringify(batch, schema, rows, columns, id)
        values <- cells$values
        truncated <- cells$truncated
      }

      visible_rows <- rows
      visible_columns <- columns
      repeat {
        output <- format_table(
          values,
          names,
          types,
          truncated,
          visible_rows,
          visible_columns,
          rows,
          total_columns,
          more_rows
        )
        if (nchar(output, type = "bytes") + 1L <= response_bytes) {
          cat(output, "\n", sep = "")
          return(invisible(NULL))
        }
        if (visible_rows > 0L) {
          visible_rows <- visible_rows - 1L
        } else if (visible_columns > 1L) {
          visible_columns <- visible_columns - 1L
        } else {
          stop("SQL preview cannot fit within the response budget")
        }
      }
    }

    evaluate_impl <- function(id) {
      tryCatch(
        {
          ensure_connection()
          if (
            !isTRUE(tryCatch(
              DBI::dbIsValid(selected_connection),
              error = function(...) FALSE
            ))
          ) {
            stop(
              paste(
                "The selected SQL connection is no longer valid;",
                "call console_sql_connection(NULL) to restore DuckDB"
              )
            )
          }

          preview <- if (identical(selected_connection, managed_connection)) {
            fetch_query()
          } else {
            fetch_dbi()
          }
          if (!is.null(preview)) {
            render_preview(preview, id)
          }
        },
        error = function(error) {
          cat("Error: ", conditionMessage(error), "\n", sep = "")
        }
      )
      invisible(NULL)
    }

    interrupted <- FALSE

    evaluate <- function(id) {
      interrupted <<- FALSE
      # Observe the condition without handling it; R_tryEval remains the boundary.
      withCallingHandlers(
        evaluate_impl(id),
        interrupt = function(condition) interrupted <<- TRUE,
        error = function(condition) interrupted <<- FALSE
      )
    }

    environment()
  },
  envir = base::new.env(parent = base::baseenv())
)
