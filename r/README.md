# mcp.console

`mcp.console` adds MCP Console's persistent R, Python, and SQL workbench to an [ellmer](https://ellmer.tidyverse.org/) chat on macOS or Linux.

```r
pak::pak("github::t-kalinowski/mcp-console/r")

library(ellmer)
library(mcp.console)

chat <- chat_openai()
chat$register_tool(console_tool())
chat$chat("Tell me something interesting about mtcars. Use the console.")
```

State persists between calls.
For `chat$chat_async()`, select `tool_mode = "sequential"` when calls depend on one another.

`console_tool()` uses the first Console executable on `PATH`, otherwise the latest published release through `reticulate::uv_run_tool()`.
Named `path=` selects an executable; named `version=` selects a published release regardless of `PATH`.
They are mutually exclusive, and `...` must be empty.

Sandboxing is enabled by default.
`no_sandbox = TRUE` skips inner native enforcement and its descendant-cleanup guarantee; an explicitly selected compute target retains its outer boundary.
See [configuration and safety](https://github.com/t-kalinowski/mcp-console#limits-and-trust-boundaries).
Garbage collection closes server input and waits up to 15 seconds before forcibly stopping the server process; that fallback is not proof of descendant cleanup.

The tool accepts the shared requirement actions: `get`, `add` (default), `set`, and `reset`.
Use `character()` for explicitly empty lists.
Inspection returns the complete declaration as JSON; changed live replacements normally need `control = "restart"`.
See [requirements](https://t-kalinowski.github.io/mcp-console/REQUIREMENTS.html) for replacement semantics and target limits.
