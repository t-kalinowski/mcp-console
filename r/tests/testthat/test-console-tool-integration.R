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

managed_python_mcp_console <- function() {
  binary <- real_mcp_console()
  directory <- tempfile("mcp-console-python-")
  dir.create(directory)
  uv <- Sys.which("uv")
  stopifnot(nzchar(uv), file.symlink(uv, file.path(directory, "uv")))
  launcher <- file.path(directory, "mcp-console")
  writeLines(
    c(
      "#!/bin/sh",
      "unset R_TESTS R_HOME RHOME RETICULATE_UV RETICULATE_PYTHON",
      sprintf("export PATH=%s", shQuote(directory)),
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
  old_environment <- Sys.getenv(
    c("MCP_CONSOLE_HOME", "R_USER_CACHE_DIR", "IR_CACHE_DIR"),
    unset = NA_character_
  )
  on.exit(
    {
      setwd(old)
      missing <- is.na(old_environment)
      Sys.unsetenv(names(old_environment)[missing])
      if (any(!missing)) {
        do.call(Sys.setenv, as.list(old_environment[!missing]))
      }
      unlink(directory, recursive = TRUE)
    },
    add = TRUE
  )
  # Pak requires an explicit cache during R CMD check. A fresh ir cache keeps
  # cached resolutions from hiding failures in the preparation path.
  Sys.setenv(
    MCP_CONSOLE_HOME = file.path(getwd(), "console"),
    R_USER_CACHE_DIR = file.path(getwd(), "r-cache"),
    IR_CACHE_DIR = file.path(getwd(), "ir-cache")
  )
  force(code)
}

mock_completion <- function(message, finish_reason) {
  list(
    id = "chatcmpl-test",
    object = "chat.completion",
    created = 0L,
    model = "mock",
    choices = list(list(
      index = 0L,
      message = message,
      finish_reason = finish_reason
    )),
    usage = list(
      prompt_tokens = 0L,
      completion_tokens = 0L,
      total_tokens = 0L
    )
  )
}

test_that("console_tool works when registered with an ellmer chat", {
  # fmt: r
  source <- r"(
    x <- 41L
    cat("adapter head\n", strrep("x", 10000), "\nadapter tail\n", sep = "")
    x + 1L
  )"
  responses <- list(
    mock_completion(
      list(
        role = "assistant",
        content = NULL,
        tool_calls = list(list(
          id = "call_1",
          type = "function",
          `function` = list(
            name = "send",
            arguments = jsonlite::toJSON(list(r = source), auto_unbox = TRUE)
          )
        ))
      ),
      "tool_calls"
    ),
    mock_completion(list(role = "assistant", content = "done"), "stop")
  )
  requests <- list()
  httr2::local_mocked_responses(function(req) {
    requests[[length(requests) + 1L]] <<- httr2::req_get_body(req)
    httr2::response_json(body = responses[[length(requests)]])
  })

  with_temp_working_directory({
    with_mcp_console_languages("r", {
      directory <- tempfile("mcp-console-path-")
      dir.create(directory)
      file.copy(bare_mcp_console(), file.path(directory, "mcp-console"))
      Sys.chmod(file.path(directory, "mcp-console"), "0755")

      with_path(directory, {
        chat <- ellmer::chat_openai_compatible(
          base_url = "https://example.test/v1",
          model = "mock",
          credentials = function() "unused",
          echo = "none"
        )
        tryCatch(
          {
            chat$register_tool(console_tool())
            text <- NULL
            chat$on_tool_result(function(result) text <<- result@value@text)

            expect_identical(
              as.character(chat$chat("Use the console.")),
              "done"
            )
            expect_named(chat$get_tools(), "send")
            expect_length(requests, 2L)
            expect_identical(
              requests[[1L]]$tools[[1L]][["function"]]$name,
              "send"
            )
            expect_match(
              jsonlite::toJSON(requests[[2L]], auto_unbox = TRUE),
              "[1] 42",
              fixed = TRUE
            )
            expect_type(text, "character")
            expect_lte(nchar(text, type = "bytes"), 8192L)
            expect_match(text, "adapter head\n", fixed = TRUE)
            expect_match(text, "adapter tail\n", fixed = TRUE)
            expect_match(text, "[output omitted: ", fixed = TRUE)
            sessions <- list.dirs("console/sessions", recursive = FALSE)
            expect_length(sessions, 1L)
            raw_log <- file.path(
              getwd(),
              sessions,
              "outputs",
              "call-000001.log"
            )
            expect_match(
              text,
              paste0(" UTF-8 bytes; raw log: ", raw_log, "]"),
              fixed = TRUE
            )
            expect_true(file.exists(raw_log))
            expect_identical(
              readChar(raw_log, file.info(raw_log)$size, useBytes = TRUE),
              paste0(
                "adapter head\n",
                strrep("x", 10000),
                "\nadapter tail\n[1] 42\n"
              )
            )
          },
          finally = {
            rm(chat)
            gc()
          }
        )
      })
    })
  })
})


inspect_requirements <- function(send) {
  deadline <- Sys.time() + 600
  text <- send(requirements = list(action = "get"), timeout_ms = 0)@text
  while (endsWith(text, "[worker starting]")) {
    remaining <- as.numeric(difftime(deadline, Sys.time(), units = "secs"))
    stopifnot(remaining > 0)
    # This is the public readiness poll. Each call waits for startup rather
    # than parsing a pending notice as a JSON requirements declaration.
    text <- send(
      requirements = list(action = "get"),
      timeout_ms = as.integer(1000 * min(60, remaining))
    )@text
  }
  jsonlite::fromJSON(text)
}


test_that("requirements actions preserve scalar fields and empty lists", {
  with_temp_working_directory({
    # Exercise declaration serialization without rebuilding unrelated R packages.
    send <- console_tool(path = managed_python_mcp_console(), no_sandbox = TRUE)
    startup <- inspect_requirements(send)
    # Inspect the committed default environment after worker readiness.
    expect_true(startup$prepared)
    prepared <- send(
      requirements = list(
        action = "set",
        r = character(),
        python = character(),
        duckdb = character(),
        python_version = ">=3.11",
        exclude_newer = "2026-01-01"
      )
    )
    expect_identical(prepared@text, "[prepared]")
    selected <- inspect_requirements(send)
    expect_length(selected$requirements$python, 0L)
    expect_identical(selected$requirements$python_version, ">=3.11")
    expect_identical(selected$requirements$exclude_newer, "2026-01-01")
    declaration <- selected$requirements
    declaration$action <- "set"
    expect_identical(send(requirements = declaration)@text, "[prepared]")
    expect_identical(
      inspect_requirements(send),
      selected
    )
  })
})
