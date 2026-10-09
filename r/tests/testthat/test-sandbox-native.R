# Opt in with MCP_CONSOLE_TEST_BINARY; once selected, setup failures are failures,
# not automatic skips or reasons to retry without a sandbox.
test_that("native sandbox captures output and forwards stdin", {
  with_sandbox_directory({
    expect_identical(sandbox_rscript('cat("out\\n")', stdout = TRUE), "out")
    expect_identical(
      sandbox_rscript(
        'cat(readLines(file("stdin")), sep = "\\n")',
        input = c("one", "two"),
        stdout = TRUE
      ),
      c("one", "two")
    )
  })
})

test_that("default policy ignores global and project configuration and denies writes", {
  with_sandbox_directory({
    dir.create(".agents/console", recursive = TRUE)
    dir.create(Sys.getenv("MCP_CONSOLE_HOME"))
    config <- c(
      "sandbox:",
      "  filesystem:",
      "    read_write: [.]",
      "  network: enabled"
    )
    writeLines(config, ".agents/console/config.yaml")
    writeLines(config, file.path(Sys.getenv("MCP_CONSOLE_HOME"), "config.yaml"))
    out <- sandbox_rscript(
      'suppressWarnings(tryCatch({writeLines("bad", "blocked"); cat("wrote\\n")}, error = function(e) cat("denied\\n")))',
      stdout = TRUE
    )
    expect_identical(out, "denied")
    expect_false(file.exists("blocked"))
    out <- sandbox_rscript(
      'writeLines("allowed", "allowed"); cat("wrote\\n")',
      stdout = TRUE,
      sandbox = sandbox_config(
        filesystem = sandbox_filesystem(read_write = ".")
      )
    )
    expect_identical(out, "wrote")
    expect_identical(readLines("allowed"), "allowed")
  })
})

test_that("Unix metacharacters cannot create a file outside the sandbox policy", {
  skip_on_os("windows")
  with_sandbox_directory({
    binary <- sandbox_test_binary()
    suppressWarnings(sandboxed_system2(
      "printf",
      c(
        shQuote("start\\n"),
        ";",
        "printf escaped > escaped"
      ),
      stdout = TRUE,
      stderr = FALSE,
      path = binary
    ))
    expect_false(file.exists("escaped"))
    sandboxed_system2(
      "printf",
      c(
        shQuote("%s\\n"),
        '"$(printf escaped > escaped)"'
      ),
      stdout = TRUE,
      stderr = FALSE,
      path = binary
    )
    expect_false(file.exists("escaped"))
  })
})

test_that("caller-authorized output streams remain usable without path grants", {
  with_sandbox_directory({
    result <- sandbox_rscript('cat("stream\\n")', stdout = "host-opened.txt")
    expect_identical(result, 0L)
    expect_identical(readLines("host-opened.txt"), "stream")
  })
})

test_that("native target argument quoting preserves standard argv values", {
  with_sandbox_directory({
    values <- c("a b", "", 'a"b', "a'b", "café 雪", "ends\\", "%PATH%", "a&b")
    out <- sandboxed_system2(
      file.path(R.home("bin"), "Rscript"),
      c("--vanilla", "-e", shQuote('dput(commandArgs(TRUE))'), shQuote(values)),
      stdout = TRUE,
      path = sandbox_test_binary()
    )
    # dput is produced by this fixed fixture, not by arbitrary external data.
    expect_identical(eval(parse(text = out), envir = baseenv()), values)
  })
})

test_that("native launch supports asynchronous policy handoff without policy files", {
  with_sandbox_directory({
    binary <- sandbox_test_binary()
    result <- sandboxed_system2(
      file.path(R.home("bin"), "Rscript"),
      c(
        "--vanilla",
        "-e",
        shQuote('Sys.sleep(0.1); writeLines("done", "done")')
      ),
      stdout = FALSE,
      stderr = "async-errors.txt",
      wait = FALSE,
      sandbox = sandbox_config(
        filesystem = sandbox_filesystem(read_write = ".")
      ),
      path = binary
    )
    expect_identical(result, 0L)
    deadline <- Sys.time() + 15
    while (!file.exists("done") && Sys.time() < deadline) {
      Sys.sleep(0.05)
    }
    expect_true(
      file.exists("done"),
      info = paste(readLines("async-errors.txt"), collapse = "\n")
    )
  })
})

test_that("unsupported managed policies fail before executing the target", {
  with_sandbox_directory({
    out <- suppressWarnings(sandbox_rscript(
      'writeLines("bad", "should-not-run")',
      stdout = TRUE,
      stderr = TRUE,
      sandbox = sandbox_config(
        filesystem = sandbox_filesystem(read_write = "."),
        network = sandbox_network(proxy = sandbox_proxy(socks5 = "tcp_udp"))
      )
    ))
    expect_false(is.null(attr(out, "status")))
    expect_false(identical(attr(out, "status"), 0L))
    expect_false(file.exists("should-not-run"))
  })
})

test_that("timeout retains base status", {
  with_sandbox_directory({
    expect_warning(
      out <- sandbox_rscript(
        'Sys.sleep(60)',
        stdout = TRUE,
        stderr = "timeout-errors",
        timeout = 3
      ),
      "timed out"
    )
    expect_identical(attr(out, "status"), 124L)
  })
})
