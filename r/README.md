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

## Typed sandbox configuration

The development interface uses S7 from `RConsortium/S7`, pinned in `DESCRIPTION`.
Install the declared GitHub dependency with pak/remotes before building the package; `R CMD INSTALL` alone does not process `Remotes`.
During development, use `path=` to select the current checkout's Console binary.
Older published binaries may not understand this schema or `--no-config`.

```r
policy <- sandbox_config(
  filesystem = sandbox_filesystem(
    read_only = "./data",
    read_write = "./output",
    deny = "./secrets"
  ),
  network = sandbox_network(
    proxy = sandbox_proxy(
      domains = sandbox_domains(allow = "api.example.com")
    )
  )
)

# Same shape and names as config.yaml's sandbox node:
as.list(policy)

# Reuse the object for a persistent tool or a one-off command:
tool <- console_tool(sandbox = policy, path = Sys.which("mcp-console"))
sandboxed_system2(
  file.path(R.home("bin"), "Rscript"),
  c("--vanilla", "-e", shQuote("cat(1 + 1, '\\n')")),
  stdout = TRUE,
  sandbox = policy,
  path = Sys.which("mcp-console")
)
```

A bare `sandbox_config()` omits all options.
It does not grant workspace writes or networking.
This is a host-read/private-write baseline, not filesystem secrecy: sensitive readable locations need explicit denial.
Create required output directories on the host before granting them; R does not create or normalize configured paths.

`console_tool(sandbox = NULL)` preserves normal global/project discovery.
Supplying an object replaces the complete worker sandbox node, while retaining other application settings, including independent resolver settings.
This still trusts the discovered application configuration; the object is not a security ceiling over executables, environment, or dependency preparation.
`sandboxed_system2()` instead uses `--no-config`, so ambient configuration cannot supply additional grants.
It accepts neither `sandbox = NULL` nor an unsandboxed fallback.

`NULL` means omitted, `sandbox_filesystem()` is an explicit empty mapping, and `read_write = character()` is an explicit empty sequence.
Serialization keeps these distinct.
The resulting node can also be placed under `resolver.sandbox`; no resolver API or resolver defaults are added by this R interface.

The command wrapper retains `system2()`'s already-quoted argument convention.
On Unix, shell fragments execute inside the sandbox.
On Windows, there is no added shell; `env` has base R's command-line-assignment limitations.
R opens `stdin`/`stdout`/`stderr` files on the host and passes those authorized streams across the boundary.
Native policy support, setup, and process retirement are still Console's responsibility.
See `?sandbox_config` and `?sandboxed_system2`.
