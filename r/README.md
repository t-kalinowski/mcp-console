# mcp.console

`mcp.console` adds MCP Console to an [ellmer](https://ellmer.tidyverse.org/) chat as a persistent workbench for R, Python, and DuckDB.

MCP Console source supports macOS and x86-64 Linux.
The currently published 0.0.2 release contains only macOS wheels; Linux publication will begin with the next release.
Windows is not supported.

Linux requires unprivileged user namespaces.
The host's security policy must allow the wheel's private Bubblewrap companion to create them; a system Bubblewrap installation is not required.
Linux also requires kernel 5.11 or newer.

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

With neither `path` nor `version` supplied, `console_tool()` uses the first `mcp-console` executable on `PATH`.
If none is found, it resolves the latest published release with `reticulate::uv_run_tool()`.

Use a specific executable directly:

```r
tool <- console_tool(path = Sys.which("mcp-console"))
```

Use a specific published release, regardless of what is on `PATH`.
The current 0.0.2 release is macOS-only:

```r
tool <- console_tool(version = "0.0.2")
```

`path` and `version` are mutually exclusive and must be named.
`...` is reserved for future use and must currently be empty.
