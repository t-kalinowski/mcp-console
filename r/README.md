# mcp.console

`mcp.console` adds MCP Console to an [ellmer](https://ellmer.tidyverse.org/) chat as a persistent workbench for R, Python, and DuckDB.

MCP Console supports macOS and Linux.
Evaluated code runs in a sandbox by default on both platforms.
Use `console_tool(no_sandbox = TRUE)` to run with the server's filesystem and network permissions.

## Install

```r
pak::pak("github::t-kalinowski/mcp-console/r")
```

## Use with ellmer

```r
library(ellmer)
library(mcp.console)

chat <- chat_openai()
chat$register_tool(console_tool())

chat$chat(
  "Tell me something interesting about mtcars. Use the console as a workbench."
)
```

The console keeps its R, Python, and DuckDB state between calls.
When using `chat$chat_async()`, set `tool_mode = "sequential"` when later calls depend on earlier ones.

When the tool is garbage collected, it closes the server's input to request shutdown and waits up to 15 seconds before forcibly stopping the server.
With sandboxing enabled, the sandbox manager owns cleanup of worker descendants; the R wrapper's fallback targets only the server process.

With neither `path` nor `version` supplied, `console_tool()` uses the first `mcp-console` executable on `PATH`.
If none is found, it resolves the latest published release with `reticulate::uv_run_tool()`.

Use a specific executable directly:

```r
tool <- console_tool(path = Sys.which("mcp-console"))
```

Use a specific published release, regardless of what is on `PATH`:

```r
tool <- console_tool(version = "0.0.2")
```

`path` and `version` are mutually exclusive and must be named.
`no_sandbox = TRUE` skips the sandbox launcher and does not guarantee cleanup of worker descendants.
`...` is reserved for future use and must currently be empty.
