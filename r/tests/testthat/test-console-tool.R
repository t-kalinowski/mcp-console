mcptools_mcp_console <- function() {
  tools_file <- normalizePath(
    testthat::test_path("fixtures", "mcptools-console-tools.R"),
    mustWork = TRUE
  )
  expression <- sprintf(
    "mcptools::mcp_server(%s, session_tools = FALSE)",
    encodeString(tools_file, quote = '"')
  )
  launcher <- tempfile("mcptools-mcp-console-")
  writeLines(
    c(
      "#!/bin/sh",
      "unset R_TESTS",
      sprintf(
        "exec %s --vanilla -e %s \"$@\"",
        shQuote(file.path(R.home("bin"), "Rscript")),
        shQuote(expression)
      )
    ),
    launcher
  )
  Sys.chmod(launcher, "0755")
  launcher
}

real_mcp_console <- function() {
  binary <- Sys.getenv("MCP_CONSOLE_TEST_BINARY")
  testthat::skip_if(
    !nzchar(binary),
    "MCP_CONSOLE_TEST_BINARY does not identify the checkout binary"
  )
  normalizePath(binary, mustWork = TRUE)
}

bare_mcp_console <- function() {
  binary <- real_mcp_console()
  # Managed resolution has its own transcript coverage. Keep the R package's
  # checkout smoke tests independent of resolver tools, caches, and the network.
  library <- tempfile("mcp-console-bare-library-")
  dir.create(library)
  launcher <- tempfile("mcp-console-bare-")
  writeLines(
    c(
      "#!/bin/sh",
      "unset R_TESTS RETICULATE_UV",
      "export PATH=/usr/bin:/bin:/usr/sbin:/sbin",
      sprintf("export R_HOME=%s", shQuote(R.home())),
      sprintf("export R_LIBS=%s", shQuote(library)),
      sprintf("export R_LIBS_USER=%s", shQuote(library)),
      sprintf("export R_LIBS_SITE=%s", shQuote(library)),
      sprintf("exec %s \"$@\"", shQuote(binary))
    ),
    launcher
  )
  Sys.chmod(launcher, "0755")
  launcher
}

with_path <- function(path, code) {
  old <- Sys.getenv("PATH")
  on.exit(Sys.setenv(PATH = old), add = TRUE)
  Sys.setenv(PATH = path)
  force(code)
}

without_mcp_console <- function(code) {
  directory <- tempfile("empty-path-")
  dir.create(directory)
  with_path(directory, code)
}

with_mcp_console_languages <- function(languages, code) {
  old <- Sys.getenv("MCP_CONSOLE_LANGUAGES", unset = NA_character_)
  on.exit(
    {
      if (is.na(old)) {
        Sys.unsetenv("MCP_CONSOLE_LANGUAGES")
      } else {
        Sys.setenv(MCP_CONSOLE_LANGUAGES = old)
      }
    },
    add = TRUE
  )
  Sys.setenv(MCP_CONSOLE_LANGUAGES = paste(languages, collapse = ","))
  force(code)
}

with_temp_working_directory <- function(code) {
  directory <- tempfile("mcp-console-test-")
  dir.create(directory)
  old <- setwd(directory)
  on.exit(setwd(old), add = TRUE)
  force(code)
}

test_that("console_tool finds mcp-console on PATH by default", {
  directory <- tempfile("mcp-console-path-")
  dir.create(directory)
  binary <- file.path(directory, "mcp-console")
  file.copy(mcptools_mcp_console(), binary)
  Sys.chmod(binary, "0755")

  with_path(directory, {
    testthat::local_mocked_bindings(
      uv_run_tool = function(...) stop("uv fallback used"),
      .package = "mcp.console"
    )

    tool <- console_tool()
    expect_equal(format(tool(r = "1 + 1")), "1 + 1")

    rm(tool)
    gc()
  })
})

test_that("console_tool falls back to uv and adapts the server schema", {
  binary <- mcptools_mcp_console()
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

  tool <- without_mcp_console(console_tool())

  expect_equal(calls$tool, "python")
  expect_equal(calls$from, "mcp-console")
  expect_match(paste(calls$args, collapse = " "), "sys.executable")
  expect_equal(tool@name, "send")
  expect_equal(tool@description, "Persistent test console.")
  expect_named(
    formals(tool),
    c("r", "sql", "control", "requirements", "stdin", "timeout_ms")
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
  expect_equal(format(tool(sql = "select 1")), "select 1")
  expect_equal(format(tool()), "<poll>")
  expect_match(
    format(tool(requirements = list(r = character()))),
    "at least one of `requirements.r`"
  )

  rm(tool)
  gc()
})

test_that("console_tool uses an explicit path", {
  binary <- mcptools_mcp_console()
  testthat::local_mocked_bindings(
    uv_run_tool = function(...) stop("uv fallback used"),
    .package = "mcp.console"
  )

  tool <- without_mcp_console(console_tool(path = binary))
  expect_equal(format(tool(r = "1 + 1")), "1 + 1")

  rm(tool)
  gc()
})

test_that("console_tool resolves an explicit version with uv", {
  binary <- mcptools_mcp_console()
  calls <- new.env(parent = emptyenv())
  testthat::local_mocked_bindings(
    uv_run_tool = function(..., from = NULL) {
      calls$from <- from
      binary
    },
    .package = "mcp.console"
  )

  directory <- tempfile("mcp-console-path-")
  dir.create(directory)
  file.copy(binary, file.path(directory, "mcp-console"))
  Sys.chmod(file.path(directory, "mcp-console"), "0755")

  tool <- with_path(directory, console_tool(version = "0.0.2"))

  expect_equal(calls$from, "mcp-console==0.0.2")
  expect_equal(format(tool(r = "1 + 1")), "1 + 1")

  rm(tool)
  gc()
})

test_that("console_tool requires empty dots", {
  expect_error(console_tool(unknown = TRUE), "`...` must be empty")
})

test_that("console_tool validates path and version", {
  expect_error(
    console_tool(path = "mcp-console", version = "0.0.2"),
    "Only one of `path` and `version`"
  )
  expect_error(console_tool(path = ""), "`path` must be NULL")
  expect_error(console_tool(version = ""), "`version` must be NULL")
  expect_error(console_tool(path = tempdir()), "existing file")
})

test_that("console_tool cancels an interrupted checkout request and continues", {
  binary <- bare_mcp_console()
  watcher_file <- normalizePath(
    testthat::test_path("fixtures", "watch-cancellation.R"),
    mustWork = TRUE
  )

  with_temp_working_directory({
    with_mcp_console_languages("r", {
      marker <- paste0("mcp-console-cancellation-marker-", Sys.getpid())
      old_marker <- Sys.getenv(
        "MCP_CONSOLE_TEST_CANCEL_MARKER",
        unset = NA_character_
      )
      on.exit(
        {
          if (is.na(old_marker)) {
            Sys.unsetenv("MCP_CONSOLE_TEST_CANCEL_MARKER")
          } else {
            Sys.setenv(MCP_CONSOLE_TEST_CANCEL_MARKER = old_marker)
          }
        },
        add = TRUE
      )
      Sys.setenv(MCP_CONSOLE_TEST_CANCEL_MARKER = marker)

      tool <- console_tool(path = binary)
      on.exit(
        {
          rm(tool)
          gc()
        },
        add = TRUE
      )
      expect_identical(format(tool(r = "invisible(NULL)")), "[done]")

      source <- paste(
        "cat(Sys.getenv('MCP_CONSOLE_TEST_CANCEL_MARKER'), '\\n')",
        "flush.console()",
        "repeat Sys.sleep(1)",
        sep = "; "
      )
      expect_match(
        format(tool(r = source, timeout_ms = 0)),
        "[running",
        fixed = TRUE
      )

      observed <- ""
      for (attempt in seq_len(30L)) {
        observed <- paste0(observed, format(tool(timeout_ms = 100)))
        if (grepl(marker, observed, fixed = TRUE)) {
          break
        }
      }
      expect_match(observed, marker, fixed = TRUE)

      ack <- tempfile("mcp-console-cancellation-watcher-")
      watcher_stdout <- tempfile(fileext = ".out")
      watcher_stderr <- tempfile(fileext = ".err")
      on.exit(
        unlink(c(ack, watcher_stdout, watcher_stderr)),
        add = TRUE
      )
      watcher <- processx::process$new(
        file.path(R.home("bin"), "Rscript"),
        c(
          "--vanilla",
          watcher_file,
          getwd(),
          marker,
          as.character(Sys.getpid()),
          ack
        ),
        stdout = watcher_stdout,
        stderr = watcher_stderr,
        cleanup = TRUE
      )
      on.exit(if (watcher$is_alive()) watcher$kill(), add = TRUE)

      condition <- tryCatch(tool(timeout_ms = 60000), interrupt = identity)
      expect_s3_class(condition, "interrupt")

      watcher$wait(5000)
      if (
        watcher$is_alive() ||
          !identical(watcher$get_exit_status(), 0L) ||
          !file.exists(ack)
      ) {
        stop(paste(
          "cancellation watcher failed:",
          paste(readLines(watcher_stdout, warn = FALSE), collapse = "\n"),
          paste(readLines(watcher_stderr, warn = FALSE), collapse = "\n")
        ))
      }

      followup <- format(tool(
        control = "interrupt",
        r = "cancel_followup <- 42L; cancel_followup",
        timeout_ms = 3000
      ))

      expect_identical(followup, "\n[1] 42\n[done]")
    })
  })
})

test_that("console_tool works with the checkout binary", {
  binary <- bare_mcp_console()

  with_temp_working_directory({
    with_mcp_console_languages(c("r", "sql"), {
      tool <- console_tool(path = binary)

      expect_named(
        formals(tool),
        c("r", "sql", "control", "stdin", "timeout_ms")
      )

      expect_equal(format(tool(r = "x <- 41; x + 1")), "[1] 42\n")
      expect_equal(format(tool(r = "x + 2")), "[1] 43\n")

      result <- tool(r = "plot(1:3); stop('boom')")
      expect_length(result, 2L)
      expect_equal(format(result[[1L]]), "Error: boom\n")
      expect_s3_class(result[[2L]], "ellmer::ContentImageInline")
      expect_equal(result[[2L]]@type, "image/png")
      expect_gt(nchar(result[[2L]]@data), 100L)

      rm(tool)
      gc()
    })
  })
})
