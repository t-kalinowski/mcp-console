sandbox_test_binary <- function() {
  binary <- Sys.getenv("MCP_CONSOLE_TEST_BINARY")
  skip_if(!nzchar(binary), "Set MCP_CONSOLE_TEST_BINARY to the checkout binary")
  normalizePath(binary, mustWork = TRUE)
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

sandbox_rscript <- function(code, ..., sandbox = sandbox_config()) {
  sandboxed_system2(
    file.path(R.home("bin"), "Rscript"),
    c("--vanilla", "-e", shQuote(code)),
    ...,
    sandbox = sandbox,
    path = sandbox_test_binary()
  )
}

# This fixture is an argv/transport probe, NOT a sandbox implementation.
# Its marker is introduced after the outer shell has launched the fake CLI.
with_fake_sandbox <- function(code) {
  skip_on_os("windows")
  directory <- tempfile("mcp-console-fake-")
  stopifnot(dir.create(directory))
  on.exit(unlink(directory, recursive = TRUE), add = TRUE)
  path <- file.path(directory, "console with spaces")
  record <- file.path(directory, "argv")
  writeLines(
    c(
      "#!/bin/sh",
      paste("printf '%s\\n' \"$@\" >", shQuote(record)),
      "while [ \"$#\" -gt 0 ] && [ \"$1\" != '--' ]; do shift; done",
      "[ \"$#\" -gt 0 ] || exit 64",
      "shift",
      "export MCP_CONSOLE_TEST_INSIDE=inside",
      "exec \"$@\""
    ),
    path
  )
  Sys.chmod(path, "0755")
  code(path, record)
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
