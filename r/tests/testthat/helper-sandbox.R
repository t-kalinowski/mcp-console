sandbox_test_binary <- function() {
  binary <- Sys.getenv("MCP_CONSOLE_TEST_BINARY")
  skip_if(!nzchar(binary), "Set MCP_CONSOLE_TEST_BINARY to the checkout binary")
  normalizePath(binary, mustWork = TRUE)
}

sandbox_test_python <- function() {
  command <- if (.Platform$OS.type == "windows") "python" else "python3"
  path <- Sys.which(command)
  skip_if(!nzchar(path), "Existing Python test requires a Python interpreter")
  unname(path)
}

sandbox_test_r <- function() {
  file.path(R.home("bin"), if (.Platform$OS.type == "windows") "R.exe" else "R")
}

with_sandbox_directory <- function(code) {
  directory <- tempfile("mcp-console-sandbox-test-")
  stopifnot(dir.create(directory))
  old <- setwd(directory)
  old_home <- Sys.getenv("MCP_CONSOLE_HOME", unset = NA_character_)
  Sys.setenv(MCP_CONSOLE_HOME = file.path(directory, "console"))
  on.exit(
    {
      if (is.na(old_home)) {
        Sys.unsetenv("MCP_CONSOLE_HOME")
      } else {
        Sys.setenv(MCP_CONSOLE_HOME = old_home)
      }
      setwd(old)
      unlink(directory, recursive = TRUE)
    },
    add = TRUE
  )
  force(code)
}

with_console_config_probe <- function(code) {
  skip_on_os("windows")
  directory <- tempfile("mcp-console-mcp-probe-")
  stopifnot(dir.create(directory))
  on.exit(unlink(directory, recursive = TRUE), add = TRUE)
  path <- file.path(directory, "console probe")
  record <- file.path(directory, "argv")
  fixture <- normalizePath(test_path("fixtures", "console-config-probe.R"))
  writeLines(
    c(
      "#!/bin/sh",
      paste(
        "exec",
        shQuote(file.path(R.home("bin"), "Rscript")),
        "--vanilla",
        shQuote(fixture),
        shQuote(record),
        '"$@"'
      )
    ),
    path
  )
  Sys.chmod(path, "0755")
  code(path, record)
}
