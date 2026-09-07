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

test_that("releasing console_tool preserves sandbox cleanup after server stalls", {
  skip_if(Sys.info()[["sysname"]] != "Darwin")
  launcher <- bare_mcp_console()
  directory <- tempfile("mcp-console-release-")
  dir.create(directory)
  interposer <- file.path(directory, "server-eof.dylib")
  processx::run(
    "cc",
    c(
      "-dynamiclib",
      "-std=c11",
      "-Wall",
      "-Wextra",
      "-Werror",
      "-o",
      interposer,
      test_path("fixtures", "server_eof_interposer.c")
    )
  )
  marker <- file.path(directory, "server-eof")
  commands <- readLines(launcher)
  commands <- append(
    commands,
    c(
      sprintf("export DYLD_INSERT_LIBRARIES=%s", shQuote(interposer)),
      sprintf("export MCP_CONSOLE_TEST_SERVER_EOF=%s", shQuote(marker))
    ),
    after = length(commands) - 1L
  )
  writeLines(commands, launcher)

  tool <- NULL
  processes <- list()
  temporary <- character()
  tryCatch(
    with_temp_working_directory({
      with_mcp_console_languages("r", {
        tool <- console_tool(path = launcher)
        information <- tool(
          r = r"(
          hold <- file.path(Sys.getenv("TMPDIR"), "descendant-hold")
          stopifnot(system2("/usr/bin/mkfifo", shQuote(hold)) == 0L)
          child <- parallel::mcparallel({
            connection <- fifo(hold, "rb", blocking = TRUE)
            readBin(connection, "raw", 1L)
          }, detached = TRUE)
          cat(Sys.getpid(), child$pid, Sys.getenv("TMPDIR"), sep = "\n")
        )"
        )@text
        information <- strsplit(information, "\n", fixed = TRUE)[[1L]]
        expect_length(information, 3L)
        worker <- ps::ps_handle(as.integer(information[[1L]]))
        descendant <- ps::ps_handle(as.integer(information[[2L]]))
        temporary <- information[[3L]]
        relay <- ps::ps_parent(worker)
        launcher_process <- ps::ps_parent(relay)
        server <- ps::ps_parent(launcher_process)
        processes <- c(list(server), ps::ps_children(server, recursive = TRUE))
        managers <- vapply(
          processes,
          function(process) "sandbox-manager" %in% ps::ps_cmdline(process),
          logical(1L)
        )
        expect_equal(sum(managers), 1L)
        expect_true(ps::ps_is_running(descendant))
        expect_true(dir.exists(temporary))
        expect_match(
          tool(r = 'readline("release: ")', timeout_ms = 1000L)@text,
          "[waiting for stdin]",
          fixed = TRUE
        )

        # The EOF checkpoint holds the server before it can start retirement.
        # Finalization must fall back to stopping that server alone; launcher
        # parent-death supervision then owns the still-active sandbox lifetime.
        tool <- NULL
        gc()
        expect_true(file.exists(marker))
        expect_true(all(ps::ps_wait(processes, timeout = 10000L)))
        expect_false(dir.exists(temporary))
      })
    }),
    finally = {
      tool <- NULL
      gc()
      for (process in processes) {
        try(
          if (ps::ps_is_running(process)) ps::ps_send_signal(process, 9L),
          silent = TRUE
        )
      }
      stopifnot(all(ps::ps_wait(processes, timeout = 10000L)))
      unlink(c(directory, launcher, temporary), recursive = TRUE)
    }
  )
})
