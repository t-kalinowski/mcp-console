# `mcp-console`

# 🚧 UNDER CONSTRUCTION 🚧

**This project is not ready for use.**

`mcp-console` is a ground-up rewrite of [`mcp-repl`](https://github.com/posit-dev/mcp-repl).
It applies the lessons learned from `mcp-repl` to a substantially different product---different enough that a new name makes sense.

The repository currently contains the initial Rust binary package.
The following commands are implemented:

```bash
mcp-console serve
mcp-console --help
mcp-console help [COMMAND]
mcp-console --version
mcp-console sandbox -- COMMAND [ARG]...
```

`mcp-console` requires a subcommand.
`mcp-console serve` runs a minimal MCP server over stdio.
Run `mcp-console --help` or `mcp-console COMMAND --help` for command-line help.
On Unix, the server starts one private embedded-R worker and registers one `send` tool with this input:

```json
{ "r": "x <- 40\nx + 2" }
```

Each call supplies one complete R code cell.
The worker parses the whole cell, evaluates its expressions sequentially at top level, and returns R console output, including every visible top-level value.
State persists across calls for the life of the server.
Incomplete input and R parse or evaluation failures are returned as tool errors rather than continuation prompts.
R must be discoverable through `R_HOME` or `PATH` and must provide a shared library.
This initial worker is a separate process but is not launched through `mcp-console sandbox` yet.
The embedded-R worker is not implemented on Windows yet.
Output from forked descendants is not supported by this initial worker.

Python, SQL, interactive input, polling, named sessions, runtime restart, and output retention are not implemented yet.

The MCP initialization identity remains `mcp-console`.
The intended default client registration name is `console`:

```bash
codex mcp add console -- mcp-console serve
```

Under Codex's current naming convention, the implemented tool is `mcp__console.send`; the planned environment and lifecycle tool will be `mcp__console.session`.

On macOS, `sandbox` launches the command under `/usr/bin/sandbox-exec`.
The command can read the host filesystem, can write regular files only in a dedicated temporary directory, and cannot access the network.
The policy also permits the device and IPC operations needed for supported R and Python workflows, including sandbox-created PTYs and Python multiprocessing semaphores.
This initial launcher waits only for the direct command.
Background descendants are unsupported: they may outlive the launcher, which attempts to remove their dedicated temporary directory on a best-effort basis when it returns.
Descendant supervision is intentionally deferred because it must account for process groups, session-detached children, signal forwarding, and PID reuse together.
Linux and Windows are not supported yet.

The proposed product and architecture remain under [`design-sketches/`](design-sketches/README.md).

## Development

Run the local checks with:

```bash
scripts/check
```

Format the repository with each installed formatter:

```bash
scripts/format
```

Formatter errors remain visible but do not stop the remaining formatters or make the script fail.

See [`tests/transcripts/README.md`](tests/transcripts/README.md) for running and authoring external server transcript tests.

## License

MCP Console is licensed under the [MIT license](LICENSE).
