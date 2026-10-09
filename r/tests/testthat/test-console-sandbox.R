test_that("console_tool transmits the same application policy object", {
  with_console_config_probe(function(path, record) {
    policy <- sandbox_config(
      filesystem = sandbox_filesystem(read_write = "./space here"),
      network = "restricted"
    )
    tool <- console_tool(path = path, sandbox = policy)
    argv <- readLines(record, warn = FALSE)
    expect_identical(argv[1:4], c("serve", "-c", "sandbox=null", "-c"))
    expect_identical(
      jsonlite::fromJSON(
        substring(argv[[5]], nchar("sandbox=") + 1L),
        simplifyVector = FALSE
      ),
      as.list(policy)
    )
    rm(tool)
    invisible(gc())
  })
})

test_that("console_tool retains existing discovery and no_sandbox behavior", {
  with_console_config_probe(function(path, record) {
    tool <- console_tool(path = path)
    expect_identical(readLines(record, warn = FALSE), "serve")
    rm(tool)
    invisible(gc())
    tool <- console_tool(path = path, no_sandbox = TRUE)
    expect_identical(
      readLines(record, warn = FALSE),
      c("serve", "--no-sandbox")
    )
    rm(tool)
    invisible(gc())
  })
})

test_that("console_tool replaces ambient grants and retains other settings", {
  with_sandbox_directory({
    binary <- sandbox_test_binary()
    dir.create(Sys.getenv("MCP_CONSOLE_HOME"))
    dir.create("allowed")
    writeLines(
      c(
        "languages: [r]",
        "r:",
        "  resolution: disabled",
        "environment:",
        "  MCP_CONSOLE_TEST_SETTING: retained",
        "sandbox:",
        "  filesystem:",
        "    read_write: [.]",
        "  network: enabled"
      ),
      file.path(Sys.getenv("MCP_CONSOLE_HOME"), "config.yaml")
    )
    send <- console_tool(
      path = binary,
      sandbox = sandbox_config(
        filesystem = sandbox_filesystem(read_write = "./allowed")
      )
    )
    expect_false("python" %in% names(formals(send)))
    # fmt: r
    source <- r"(
      writeLines("allowed", "allowed/result")
      suppressWarnings(tryCatch(
        writeLines("ambient grant survived", "blocked"),
        error = function(e) cat("denied\n")
      ))
      cat(Sys.getenv("MCP_CONSOLE_TEST_SETTING"), "\n", sep = "")
    )"
    result <- send(r = source, timeout_ms = 60000L)
    expect_identical(result@text, "denied\nretained\n")
    expect_identical(readLines("allowed/result"), "allowed")
    expect_false(file.exists("blocked"))
    declaration <- send(
      requirements = list(action = "get"),
      timeout_ms = 60000L
    )
    expect_length(jsonlite::fromJSON(declaration@text)$requirements$r, 0L)
    rm(send)
    invisible(gc())
  })
})
