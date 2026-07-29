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
The server registers one `send` tool with two mutually exclusive inputs:

```json
{ "r": "x <- 40\nx + 2" }
```

```json
{ "stdin": "Ada\n" }
```

Each `r` call supplies one complete R code cell.
The first `r` call lazily starts one private embedded-R worker.
MCP initialization and tool listing do not require R.
The worker parses the whole cell, evaluates its expressions sequentially at top level, and returns R console output, including every visible top-level value.
State persists across calls for the life of the server.
Incomplete source is a parse error rather than a continuation prompt.
R parse, evaluation, and auto-print failures are normal language outcomes with `isError: false`; worker startup, sandbox, process, and private-protocol failures are tool errors.
Successful silent evaluations return `[done]`.

When active R code calls `readline()` or enters `browser()`, the response ends in `[input]`.
A later `stdin` call appends exact text to that evaluation without adding a newline.
Partial and multiple lines are buffered, and unused text is discarded when the evaluation ends.
New R cells are rejected while input is required, and `stdin` is rejected at other times.
If the worker stops, later calls report the stopped state rather than starting a replacement.

R must be discoverable through `R_HOME` or `PATH` and must provide a shared library.
On macOS, the worker runs under the same read-only Seatbelt policy as the `sandbox` command.
It can read host files, write regular files only in its private temporary directory, and cannot use the network.
Its descendants inherit that policy.
Platforms without an implemented worker sandbox reject `r` calls instead of starting R without one.
MCP shutdown asks the direct worker to exit, then terminates and reaps it after a one-second grace period.

Python, SQL, polling, interrupts, named sessions, runtime restart, and output retention are not implemented yet.

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

## Native R comparison snapshot

The following observations come from three consecutive local Apple Silicon macOS release-build runs on July 29, 2026.
They are a comparison snapshot, not a general benchmark:

- Functions parsed from submitted text had no source filename: `getSrcFilename()` returned an empty value.
- A top-level task callback received the internal `base::get(...)` value-proxy expression used for native auto-print bookkeeping.
- `system("printf child-output")` returned `[done]`; subprocess output written directly to the worker's standard output did not enter the MCP result.
- Fresh-process MCP launch through initialization took 4.9--7.2 ms after the executable was cached; the first run immediately after rebuilding took 253 ms.
  First R evaluation took 155--177 ms.
  Twenty steady silent evaluations had a 0.086--0.089 ms median round trip.
- After the first evaluation there were two direct processes.
  The server used 6 threads and about 4,352--4,416 KiB RSS; the R worker used 1 thread and about 76,160--76,624 KiB RSS, for about 80,576--80,976 KiB total RSS.
- The release binary was 5,332,272 bytes, about 5.09 MiB.
- A sandboxed worker is implemented only on macOS.

Remaining native-worker limitations include missing submitted-source filenames, value-proxy expressions in task callbacks, uncaptured direct child-process output, and no supervision or cleanup of worker descendants after direct-worker termination.
Forked descendants cannot use the private sideband and therefore cannot contribute console output.

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
