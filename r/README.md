# mcp.console

`mcp.console` adds Console's persistent R, Python, and SQL workbench to an [ellmer](https://ellmer.tidyverse.org/) chat.

```r
pak::pak("github::t-kalinowski/mcp-console/r")

library(ellmer)
library(mcp.console)

chat <- chat_openai()
chat$register_tool(console_tool())
chat$chat("Explore mtcars with the console. Keep the data for follow-up.")
```

State persists while the tool's server connection remains alive.
For dependent calls through `chat$chat_async()`, select `tool_mode = "sequential"`.

## Select Console

`console_tool()` uses Console on PATH, falling back to the latest published release through `reticulate::uv_run_tool()`.
Named `path` selects an executable; named `version` selects a published release regardless of PATH.
They are mutually exclusive, and `...` must be empty.

```r
console_tool(path = Sys.which("mcp-console"))
```

Native Windows support is experimental and requires a locally installed Console because the release workflow does not publish Windows wheels.
See [Windows installation](https://t-kalinowski.github.io/mcp-console/WINDOWS.html).

## Behavior and safety

The tool uses the connected server's public `send` schema.
Requirement actions are `get`, `add`, `set`, and `reset`; use `character()` for an explicitly empty list.
Changed live replacements normally require restart.
Read [Requirements](https://t-kalinowski.github.io/mcp-console/REQUIREMENTS.html) before replacing a declaration.

Sandboxing is enabled by default.
`no_sandbox = TRUE` removes native enforcement and its descendant-cleanup guarantee.
Read [Sandbox and trust](https://t-kalinowski.github.io/mcp-console/SANDBOX.html) for default host reads, preparation permissions, and cleanup limits.

Garbage collection requests shutdown by closing server input, waits up to 15 seconds, then may forcibly stop the server process.
That fallback does not prove descendant cleanup.
Keep the tool reachable while the chat is using it.

[Console guides](https://t-kalinowski.github.io/mcp-console/README.html) cover runtime behavior, configuration, input, plots, and recordings.
The package reference documents the exact R signature.
