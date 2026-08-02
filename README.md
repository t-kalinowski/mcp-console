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
The server registers one `send` tool.
Supplying `r` evaluates one complete code cell and waits up to the optional `timeout_ms`, which defaults to 60 seconds.
When that wait expires, the call returns `[running]` while computation continues; call `send` without `r` to poll for completion.
A call may also supply exact standard-input text:

```json
{ "r": "readline('name> ')", "stdin": "Ada\n" }
```

The server sends the cell first, then queues the string's UTF-8 bytes to worker fd 0 from an independent writer.
It does not inspect the text, add a newline, impose a line-size limit, or wait for an input request.
Ending a payload does not close fd 0 or produce EOF, so a newline-free fragment waits for later stdin.
The R console callback stops after one newline or a full callback buffer, leaving later bytes on fd 0 for subsequent console or direct readers.
When the worker reports an input request, `send` returns its prompt and `[input]`; a later call can append more bytes while the cell remains active:

```json
{ "stdin": "Ada\n" }
```

Queued input is not an acknowledgment of consumption.
If the same call just queued nonempty stdin, it waits for completion until its deadline before returning that input boundary.
The server does not drain unread bytes when evaluation ends, so they may satisfy a later worker read or evaluation.
On macOS, the first evaluation lazily starts a sandboxed embedded R worker.
Later calls reuse the same global R state.
The worker runs each cell through R's native top-level loop, captures R console output, prints each visible value, and maintains `.Last.value`.
If a cell ends while an expression is incomplete, earlier complete expressions from that cell remain applied.
Its MCP initialization identity remains `mcp-console`.
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
The `r` suite exercises the built-in worker.
The `zod` suite uses the hidden `serve --worker PATH` development option to exercise the same protocol with an executable Python fixture.
Both suites run on macOS, where the sandbox policy is implemented.
See [`docs/WORKER_PROTOCOL.md`](docs/WORKER_PROTOCOL.md) for the exact implemented launch and message contract.

## License

MCP Console is licensed under the [MIT license](LICENSE).
