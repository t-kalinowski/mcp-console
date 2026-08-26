fake_mcp_console <- function(languages = c("r", "python", "sql")) {
  stopifnot(
    length(languages) > 0L,
    all(languages %in% c("r", "python", "sql"))
  )

  server <- tempfile("fake-mcp-console-server-", fileext = ".R")
  writeLines(
    c(
      "#!/usr/bin/env Rscript",
      "suppressPackageStartupMessages(library(jsonlite))",
      "empty_object <- function() structure(list(), names = character())",
      sprintf(
        "languages <- c(%s)",
        paste(encodeString(languages, quote = '"'), collapse = ", ")
      ),
      sprintf("parent_pid <- %dL", Sys.getpid()),
      "input <- file('stdin')",
      "repeat {",
      "  line <- readLines(input, n = 1L, warn = FALSE)",
      "  if (length(line) == 0L) break",
      "  request <- fromJSON(line, simplifyVector = FALSE)",
      "  if (identical(request$method, 'notifications/initialized')) next",
      "  interrupt_parent <- FALSE",
      "  if (identical(request$method, 'initialize')) {",
      "    result <- list(",
      "      protocolVersion = '2025-11-25',",
      "      capabilities = list(tools = empty_object()),",
      "      serverInfo = list(name = 'mcp-console', version = 'test')",
      "    )",
      "  } else if (identical(request$method, 'tools/list')) {",
      "    optional <- function(type, description) {",
      "      list(type = type, description = description)",
      "    }",
      "    properties <- list(",
      "      r = optional(list('string', 'null'), 'R code'),",
      "      python = optional(list('string', 'null'), 'Python code'),",
      "      sql = optional(list('string', 'null'), 'SQL code'),",
      "      control = list(",
      "        type = 'string',",
      "        enum = list('interrupt', 'restart')",
      "      ),",
      "      requirements = optional(list('object', 'null'), 'Requirements'),",
      "      stdin = optional(list('string', 'null'), 'Standard input'),",
      "      timeout_ms = list(type = 'integer', minimum = 0, default = 60000)",
      "    )",
      "    properties$requirements$properties <- list(",
      "      duckdb = list(type = 'array', items = list(type = 'string')),",
      "      r = list(type = 'array', items = list(type = 'string')),",
      "      python = list(type = 'array', items = list(type = 'string'))",
      "    )",
      "    properties$requirements$additionalProperties <- FALSE",
      "    properties <- properties[c(",
      "      languages, 'control', 'requirements', 'stdin', 'timeout_ms'",
      "    )]",
      "    result <- list(tools = list(list(",
      "      name = 'send',",
      "      description = 'Persistent test console.',",
      "      inputSchema = list(type = 'object', properties = properties)",
      "    )))",
      "  } else if (identical(request$method, 'tools/call')) {",
      "    arguments <- request$params$arguments",
      "    if (!is.null(arguments$requirements)) {",
      "      kind <- if (is.list(arguments$requirements$r)) 'array' else 'scalar'",
      "      text <- paste(",
      "        kind,",
      "        arguments$requirements$r[[1]],",
      "        paste(names(arguments$requirements), collapse = ','),",
      "        sep = ':'",
      "      )",
      "    } else {",
      "      text <- if (is.null(arguments$r)) '<poll>' else arguments$r",
      "    }",
      "    content <- list(list(type = 'text', text = text))",
      "    is_error <- FALSE",
      "    if (identical(arguments$r, 'plot()')) {",
      "      content <- c(content, list(list(",
      "        type = 'image',",
      "        mimeType = 'image/png',",
      "        data = strrep('a', 100000L)",
      "      )))",
      "    }",
      "    if (identical(arguments$r, 'error_plot()')) {",
      "      content <- list(",
      "        list(type = 'text', text = 'before'),",
      "        list(type = 'image', mimeType = 'image/png', data = 'image'),",
      "        list(type = 'text', text = 'after')",
      "      )",
      "      is_error <- TRUE",
      "    }",
      "    interrupt_parent <- identical(arguments$r, 'interrupt()')",
      "    result <- list(content = content, isError = is_error)",
      "  } else {",
      "    stop('unexpected method')",
      "  }",
      "  if (isTRUE(interrupt_parent)) tools::pskill(parent_pid, tools::SIGINT)",
      "  response <- list(jsonrpc = '2.0', id = request$id, result = result)",
      "  cat(toJSON(response, auto_unbox = TRUE, null = 'null'), '\n', sep = '')",
      "  flush(stdout())",
      "}"
    ),
    server
  )
  launcher <- tempfile("fake-mcp-console-")
  writeLines(
    c(
      "#!/bin/sh",
      "unset R_TESTS",
      sprintf(
        "exec %s --vanilla %s \"$@\"",
        shQuote(file.path(R.home("bin"), "Rscript")),
        shQuote(server)
      )
    ),
    launcher
  )
  Sys.chmod(launcher, "0755")
  launcher
}

test_that("mcp_console_tool resolves, initializes, and forwards calls", {
  binary <- fake_mcp_console()
  calls <- new.env(parent = emptyenv())
  testthat::local_mocked_bindings(
    uv_run_tool = function(tool, args = character(), ..., from = NULL) {
      calls$tool <- tool
      calls$args <- args
      calls$from <- from
      binary
    },
    .package = "mcp.console"
  )

  tool <- mcp_console_tool(from = "mcp-console==test")

  expect_equal(calls$tool, "python")
  expect_equal(calls$from, "mcp-console==test")
  expect_match(paste(calls$args, collapse = " "), "sys.executable")
  expect_equal(tool@name, "send")
  expect_equal(tool@description, "Persistent test console.")
  expect_named(
    formals(tool),
    c("r", "python", "sql", "control", "requirements", "stdin", "timeout_ms")
  )
  for (argument in tool@arguments@properties) {
    expect_false("null" %in% unlist(argument@json$type, use.names = FALSE))
    expect_false(argument@required)
  }
  expect_equal(
    tool@arguments@properties$requirements@json$required,
    as.list(c("duckdb", "r", "python"))
  )
  for (requirement in tool@arguments@properties$requirements@json$properties) {
    expect_true("null" %in% unlist(requirement$type, use.names = FALSE))
  }
  expect_true(any(vapply(
    tool@arguments@properties$control@json$enum,
    is.null,
    logical(1)
  )))
  expect_equal(format(tool(r = "1 + 1")), "1 + 1")
  expect_equal(format(tool()), "<poll>")
  expect_equal(
    format(tool(
      requirements = list(
        duckdb = NULL,
        r = "dplyr",
        python = NULL
      )
    )),
    "array:dplyr:r"
  )

  result <- tool(r = "plot()")
  expect_length(result, 2L)
  expect_equal(format(result[[1L]]), "plot()")
  expect_equal(result[[2L]]@type, "image/png")
  expect_equal(nchar(result[[2L]]@data), 100000L)

  rm(tool)
  gc()
})

test_that("mcp_console_tool uses the server's configured language fields", {
  binary <- fake_mcp_console(c("r", "sql"))
  testthat::local_mocked_bindings(
    uv_run_tool = function(...) binary,
    .package = "mcp.console"
  )

  tool <- mcp_console_tool(from = "mcp-console==test")

  expect_named(
    formals(tool),
    c("r", "sql", "control", "requirements", "stdin", "timeout_ms")
  )
  expect_equal(format(tool(r = "1 + 1")), "1 + 1")

  rm(tool)
  gc()
})

test_that("mcp_console_tool preserves content from failed calls", {
  binary <- fake_mcp_console()
  testthat::local_mocked_bindings(
    uv_run_tool = function(...) binary,
    .package = "mcp.console"
  )

  tool <- mcp_console_tool(from = "mcp-console==test")
  result <- tool(r = "error_plot()")

  expect_length(result, 3L)
  expect_equal(format(result[[1L]]), "before")
  expect_s3_class(result[[2L]], "ellmer::ContentImageInline")
  expect_equal(result[[2L]]@data, "image")
  expect_equal(format(result[[3L]]), "after")

  rm(tool)
  gc()
})

test_that("mcp_console_tool invalidates an interrupted client", {
  binary <- fake_mcp_console()
  testthat::local_mocked_bindings(
    uv_run_tool = function(...) binary,
    .package = "mcp.console"
  )

  tool <- mcp_console_tool(from = "mcp-console==test")
  condition <- tryCatch(tool(r = "interrupt()"), interrupt = identity)

  expect_s3_class(condition, "interrupt")
  expect_error(tool(r = "1 + 1"), "mcp-console is not running", fixed = TRUE)

  rm(tool)
  gc()
})
