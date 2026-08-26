# mcp.console

`mcp.console` adds MCP Console to an [ellmer](https://ellmer.tidyverse.org/) chat as a persistent workbench for R, Python, and DuckDB.

MCP Console currently supports macOS only.

## Install

```r
pak::pak("github::t-kalinowski/mcp-console/r")
```

## Use with ellmer

```r
library(ellmer)
library(mcp.console)

chat <- chat_openai()
chat$register_tool(mcp_console_tool())

chat$chat(
  "Tell me something interesting about mtcars. Use the console as a workbench."
)
```

The console keeps its R, Python, and DuckDB state between calls.
When using `chat$chat_async()`, set `tool_mode = "sequential"` when later calls depend on earlier ones.

After interrupting a tool call, construct and register a new tool before continuing.

Pin a specific MCP Console version if needed:

```r
tool <- mcp_console_tool(from = "mcp-console==0.0.2")
```
