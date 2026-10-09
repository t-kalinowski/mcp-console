test_that("system2 arguments keep their names, order, and defaults", {
  reference <- formals(base::system2)
  actual <- formals(sandboxed_system2)
  expect_identical(actual[names(reference)], as.list(reference))
  expect_identical(names(actual)[seq_along(reference)], names(reference))
})

test_that("the launcher receives one JSON node with safely quoted arguments", {
  with_fake_sandbox(function(path, record) {
    policy <- SandboxPolicy(
      filesystem = SandboxFilesystem(
        read_write = c("./spaces here", "./a'b\"c;$HOME", "./café 雪")
      )
    )
    out <- sandboxed_system2(
      "printf",
      c(shQuote("%s\\n"), shQuote("a b '$")),
      stdout = TRUE,
      sandbox = policy,
      path = path
    )
    expect_identical(out, "a b '$")
    argv <- readLines(record, warn = FALSE)
    expect_identical(
      argv[1:5],
      c("sandbox", "--no-config", "-c", "sandbox=null", "-c")
    )
    node <- jsonlite::fromJSON(
      substring(argv[[6]], nchar("sandbox=") + 1L),
      simplifyVector = FALSE
    )
    expect_identical(node, as.list(policy))
    expect_identical(argv[7:9], c("--", "/bin/sh", "-c"))
  })
})

test_that("Unix shell operators and env fragments are evaluated inside the target", {
  with_fake_sandbox(function(path, record) {
    out <- sandboxed_system2(
      "printf",
      c(
        shQuote("first\\n"),
        ";",
        "printf '%s\\n' \"$MCP_CONSOLE_TEST_INSIDE\""
      ),
      stdout = TRUE,
      path = path
    )
    expect_identical(out, c("first", "inside"))
    out <- sandboxed_system2(
      "printf",
      c(
        shQuote("%s\\n"),
        "\"$(printf %s \"$MCP_CONSOLE_TEST_INSIDE\")\""
      ),
      stdout = TRUE,
      path = path
    )
    expect_identical(out, "inside")
    out <- sandboxed_system2(
      "printenv",
      "SANDBOX_VALUE",
      env = paste0("SANDBOX_VALUE=", shQuote("a b '$; literal")),
      stdout = TRUE,
      path = path
    )
    expect_identical(out, "a b '$; literal")
  })
})

test_that("the wrapper preserves base stream, input, and status behavior", {
  with_fake_sandbox(function(path, record) {
    expect_identical(
      sandboxed_system2(
        "cat",
        input = c("one", "two"),
        stdout = TRUE,
        path = path
      ),
      c("one", "two")
    )
    status <- withVisible(sandboxed_system2("true", path = path))
    expect_identical(status, list(value = 0L, visible = FALSE))
    expect_warning(
      out <- sandboxed_system2(
        "sh",
        c("-c", shQuote("exit 7")),
        stdout = TRUE,
        path = path
      ),
      "status 7"
    )
    expect_identical(attr(out, "status"), 7L)
    filename <- tempfile("sandbox-output-")
    on.exit(unlink(filename), add = TRUE)
    sandboxed_system2(
      "printf",
      shQuote("file\\n"),
      stdout = filename,
      path = path
    )
    expect_identical(readLines(filename), "file")
  })
})

test_that("asynchronous input fails before executable resolution", {
  expect_error(
    sandboxed_system2("cat", input = "one", wait = FALSE, path = "missing"),
    "`input` requires `wait = TRUE`",
    fixed = TRUE
  )
})
