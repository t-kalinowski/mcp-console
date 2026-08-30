base::local(
  {
    managed_connection <- NULL
    selected_connection <- NULL
    selected_backend <- NULL
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

    select_managed_connection <- function() {
      selected_connection <<- ensure_managed_connection()
      selected_backend <<- "managed"
      invisible(selected_connection)
    }

    ensure_connection <- function() {
      if (is.null(selected_connection)) {
        select_managed_connection()
      }
      selected_connection
    }

    sql_connection <- function() {
      ensure_connection()
    }

    python_error_message <- function(error) {
      detail <- tryCatch(
        reticulate::py_last_error(),
        error = function(...) NULL
      )
      if (
        !is.null(detail) &&
          is.character(detail$value) &&
          length(detail$value) == 1L &&
          nzchar(detail$value)
      ) {
        return(detail$value)
      }
      conditionMessage(error)
    }

    is_python_connection <- function(connection) {
      if (!inherits(connection, "python.builtin.object")) {
        return(FALSE)
      }
      isTRUE(tryCatch(
        {
          cursor <- reticulate::py_get_attr(connection, "cursor")
          reticulate::import_builtins(convert = TRUE)$callable(cursor)
        },
        error = function(...) FALSE
      ))
    }

    console_sql_connection <- function(connection) {
      if (is.null(connection)) {
        return(select_managed_connection())
      }
      if (
        inherits(connection, "DBIConnection") &&
          isTRUE(tryCatch(
            DBI::dbIsValid(connection),
            error = function(...) FALSE
          ))
      ) {
        selected_connection <<- connection
        selected_backend <<- if (identical(connection, managed_connection)) {
          "managed"
        } else {
          "dbi"
        }
        return(invisible(selected_connection))
      }
      if (is_python_connection(connection)) {
        selected_connection <<- connection
        selected_backend <<- "dbapi"
        return(invisible(selected_connection))
      }
      stop(
        paste(
          "`connection` must be a valid DBIConnection,",
          "a Python DB-API connection, or NULL"
        ),
        call. = FALSE
      )
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

    install_python_tool <- function() {
      callback <- reticulate::py_func(function(connection = NULL) {
        console_sql_connection(connection)
        invisible(NULL)
      })
      reticulate::py_set_attr(
        reticulate::import_main(convert = FALSE),
        "console_sql_connection",
        callback
      )
      invisible()
    }

    install_python_hooks <- function(...) {
      namespace <- asNamespace("reticulate")
      base::setHook(
        "reticulate.onPyInit",
        install_python_tool,
        action = "append"
      )
      if (get("is_python_initialized", envir = namespace)()) {
        install_python_tool()
      }
      invisible()
    }
    setHook(
      packageEvent("reticulate", "onLoad"),
      install_python_hooks,
      action = "append"
    )
    if ("reticulate" %in% loadedNamespaces()) {
      install_python_hooks()
    }

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

    dbapi_is_null <- function(value) {
      is.null(value) ||
        (
          length(value) == 1L &&
            is.atomic(value) &&
            isTRUE(is.na(value))
        )
    }

    dbapi_value_type <- function(value) {
      if (dbapi_is_null(value)) {
        return(NULL)
      }
      if (is.logical(value)) {
        return("bool")
      }
      if (is.integer(value)) {
        return("int64")
      }
      if (is.numeric(value)) {
        return("double")
      }
      if (is.character(value)) {
        return("string")
      }
      if (is.raw(value)) {
        return("binary")
      }
      if (inherits(value, "Date")) {
        return("date32")
      }
      if (inherits(value, "POSIXt")) {
        return("timestamp")
      }
      if (inherits(value, "difftime")) {
        return("duration")
      }
      classes <- class(value)
      python_class <- classes[startsWith(classes, "python.")][1L]
      if (!is.na(python_class)) {
        return(sub("^python\\.(builtin\\.)?", "", python_class))
      }
      "object"
    }

    dbapi_column_type <- function(values) {
      types <- unique(Filter(
        Negate(is.null),
        lapply(values, dbapi_value_type)
      ))
      if (length(types) == 0L) {
        return("unknown")
      }
      if (all(types %in% c("int64", "double"))) {
        return(if ("double" %in% types) "double" else "int64")
      }
      if (length(types) == 1L) {
        return(types[[1L]])
      }
      "object"
    }

    dbapi_cell <- function(value) {
      if (dbapi_is_null(value)) {
        return(list(value = NA_character_, truncated = FALSE))
      }
      text <- if (is.raw(value)) {
        paste0(
          "b'",
          paste(sprintf("\\x%02x", as.integer(value)), collapse = ""),
          "'"
        )
      } else if (inherits(value, "python.builtin.object")) {
        reticulate::py_str(value)
      } else if (length(value) == 1L) {
        as.character(value)
      } else {
        paste(as.character(value), collapse = ", ")
      }
      truncated <- nchar(text, type = "chars") > cell_width
      if (truncated) {
        text <- paste0(substr(text, 1L, cell_width - 1L), "…")
      }
      list(value = text, truncated = truncated)
    }

    fetch_dbapi <- function() {
      reticulate::py_clear_last_error()
      cursor <- NULL
      on.exit(
        if (!is.null(cursor)) {
          try(
            reticulate::py_call(
              reticulate::py_get_attr(cursor, "close")
            ),
            silent = TRUE
          )
        },
        add = TRUE
      )

      tryCatch(
        {
          cursor <- reticulate::py_call(
            reticulate::py_get_attr(selected_connection, "cursor")
          )
          invisible(reticulate::py_call(
            reticulate::py_get_attr(cursor, "execute"),
            source
          ))
          description <- reticulate::py_to_r(
            reticulate::py_get_attr(cursor, "description")
          )
          if (is.null(description) || length(description) == 0L) {
            return(NULL)
          }

          rows <- reticulate::py_to_r(reticulate::py_call(
            reticulate::py_get_attr(cursor, "fetchmany"),
            preview_rows + 1L
          ))
          fetched_rows <- length(rows)
          rows <- rows[seq_len(min(fetched_rows, preview_rows))]
          total_columns <- length(description)
          columns <- min(total_columns, preview_columns)
          names <- vapply(
            description[seq_len(columns)],
            function(column) as.character(column[[1L]]),
            character(1)
          )
          column_values <- lapply(seq_len(columns), function(column) {
            lapply(rows, function(row) row[[column]])
          })
          cells <- lapply(column_values, function(values) {
            lapply(values, dbapi_cell)
          })
          values <- lapply(cells, function(column) {
            vapply(column, `[[`, character(1), "value")
          })
          truncated <- lapply(cells, function(column) {
            vapply(column, `[[`, logical(1), "truncated")
          })
          types <- vapply(
            column_values,
            dbapi_column_type,
            character(1)
          )
          list(
            values = values,
            names = names,
            types = types,
            truncated = truncated,
            rows = length(rows),
            total_columns = total_columns,
            more_rows = fetched_rows > preview_rows
          )
        },
        error = function(error) {
          stop(python_error_message(error), call. = FALSE)
        }
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

    render_table <- function(
      values,
      names,
      types,
      truncated,
      rows,
      total_columns,
      more_rows
    ) {
      visible_rows <- rows
      visible_columns <- length(values)
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

      render_table(
        values,
        names,
        types,
        truncated,
        rows,
        total_columns,
        more_rows
      )
    }

    evaluate_impl <- function(id) {
      tryCatch(
        {
          ensure_connection()
          if (
            selected_backend != "dbapi" &&
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

          if (selected_backend == "managed") {
            preview <- fetch_query()
            if (!is.null(preview)) {
              render_preview(preview, id)
            }
          } else if (selected_backend == "dbi") {
            preview <- fetch_dbi()
            if (!is.null(preview)) {
              render_preview(preview, id)
            }
          } else if (selected_backend == "dbapi") {
            preview <- fetch_dbapi()
            if (!is.null(preview)) {
              render_table(
                preview$values,
                preview$names,
                preview$types,
                preview$truncated,
                preview$rows,
                preview$total_columns,
                preview$more_rows
              )
            }
          } else {
            stop("unknown SQL connection backend")
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
