args <- commandArgs(trailingOnly = TRUE)
console <- if (length(args)) args[[1L]] else "mcp-console"
config <- list(
  version = 2L,
  filesystem = list(
    kind = "restricted",
    entries = list(list(
      path = list(type = "special", value = list(kind = "root")),
      access = "read"
    ))
  ),
  network = "restricted",
  environment = list(MESSAGE = 'value: café 雪, "quotes", $() and spaces')
)
result <- processx::run(
  console,
  c(
    "sandbox",
    "--config-env",
    "SANDBOX_POLICY",
    "--",
    "/bin/sh",
    "-c",
    'printf "%s\\n" "$MESSAGE" "$1"',
    "sh",
    "literal argument: * ; $(echo no)"
  ),
  env = c(
    "current",
    SANDBOX_POLICY = jsonlite::toJSON(config, auto_unbox = TRUE)
  )
)
cat(result$stdout)
