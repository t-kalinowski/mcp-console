fake_mcp_console <- function() {
  server <- tempfile("fake-mcp-console-", fileext = ".R")
  writeLines(c(
    "#!/usr/bin/env Rscript",
    "suppressPackageStartupMessages(library(jsonlite))",
    "empty_object <- function() structure(list(), names = character())",
    "input <- stdin()",
    "repeat {",
    "  line <- readLines(input, n = 1L, warn = FALSE)",
    "  if (length(line) == 0L) break",
    "  request <- fromJSON(line, simplifyVector = FALSE)",
    "  if (identical(request$method, 'notifications/initialized')) next",
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
    "    result <- list(tools = list(list(",
    "      name = 'send',",
    "      description = 'Persistent test console.',",
    "      inputSchema = list(type = 'object', properties = properties)",
    "    )))",
    "  } else if (identical(request$method, 'tools/call')) {",
    "    arguments <- request$params$arguments",
    "    if (!is.null(arguments$requirements)) {",
    "      kind <- if (is.list(arguments$requirements$r)) 'array' else 'scalar'",
    "      text <- paste(kind, arguments$requirements$r[[1]], sep = ':')",
    "    } else {",
    "      text <- if (is.null(arguments$r)) '<poll>' else arguments$r",
    "    }",
    "    content <- list(list(type = 'text', text = text))",
    "    if (identical(arguments$r, 'plot()')) {",
    "      content <- c(content, list(list(",
    "        type = 'image',",
    "        mimeType = 'image/png',",
    "        data = strrep('a', 100000L)",
    "      )))",
    "    }",
    "    result <- list(content = content, isError = FALSE)",
    "  } else {",
    "    stop('unexpected method')",
    "  }",
    "  response <- list(jsonrpc = '2.0', id = request$id, result = result)",
    "  cat(toJSON(response, auto_unbox = TRUE, null = 'null'), '\n', sep = '')",
    "  flush(stdout())",
    "}"
  ), server)
  Sys.chmod(server, "0755")
  server
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
    .package = "mcpconsole"
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
  expect_equal(format(tool(r = "1 + 1")), "1 + 1")
  expect_equal(format(tool()), "<poll>")
  expect_equal(format(tool(requirements = list(r = "dplyr"))), "array:dplyr")

  result <- tool(r = "plot()")
  expect_length(result, 2L)
  expect_equal(format(result[[1L]]), "plot()")
  expect_equal(result[[2L]]@type, "image/png")
  expect_equal(nchar(result[[2L]]@data), 100000L)

  rm(tool)
  gc()
})
