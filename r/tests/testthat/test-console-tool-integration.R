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
  on.exit(setwd(old), add = TRUE)
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
            arguments = '{"r":"x <- 41L; x + 1L"}'
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
