# mcpconsole

`mcpconsole` is a thin R wrapper that registers the persistent
`mcp-console serve` session as an [ellmer](https://ellmer.tidyverse.org/) tool.
It resolves the `mcp-console` binary with `reticulate::uv_run_tool()` and keeps
the server process alive with processx.

MCP Console currently supports macOS only.

## Install

```r
pak::pak("mcpconsole=github::t-kalinowski/mcp-console/r")
```

## Use with ellmer

```r
library(ellmer)
library(mcpconsole)

chat <- chat_openai()
chat$register_tool(mcp_console_tool())

chat$chat(paste(
  "Use the console to simulate 100,000 standard normal values in R,",
  "then report their mean and standard deviation."
))
```

The returned tool owns one persistent MCP Console process. Synchronous
`chat$chat()` calls execute tools sequentially. When using
`chat$chat_async()`, set `tool_mode = "sequential"` so calls that depend on the
live R, Python, or DuckDB state retain their intended order.

Interrupting an in-progress call from R closes its server process so a later call cannot consume an unread response.
Construct and register a new tool before continuing.

Pass a Python requirement to pin the executable version:

```r
tool <- mcp_console_tool(from = "mcp-console==0.0.2")
```
