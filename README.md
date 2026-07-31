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
`mcp-console serve` runs the MCP server over stdio.
Run `mcp-console --help` or `mcp-console COMMAND --help` for command-line help.
Its MCP initialization identity remains `mcp-console`.
The intended default client registration name is `console`:

```bash
codex mcp add console -- mcp-console serve
```

Under Codex's current naming convention, the implemented tool is `mcp__console.send`; the planned environment and lifecycle tool will be `mcp__console.session`.
The current runtime spike gives `send` two inputs:

```json
{ "r": "x <- 40\nx + 2" }
```

```json
{ "stdin": "c\n" }
```

On macOS, the first R call starts one embedded Ark worker in console mode.
R state persists across calls.
Each complete R cell uses console semantics: visible top-level expressions are printed in order, invisible expressions are omitted, and evaluation stops at the first R error.
A call waits until evaluation completes or Ark requests input.
Useful output and R errors are returned directly, `[done]` represents completion without useful text, and an input boundary ends with `[input]`.
`stdin` resumes `readline()`, `browser()`, `recover()`, and menu prompts; newlines are significant and are not added automatically.

The server launches the same `mcp-console` executable in its private hidden `worker` subcommand.
That worker links Ark as a Rust crate and embeds R on its main thread; no separate `ark` executable or kernelspec is used.
The worker runs under a per-session macOS Seatbelt policy.
It communicates with the server through private Jupyter channels over ZeroMQ Unix-domain sockets beneath a short, unique directory in `/tmp`.
The worker and its descendants can read the host filesystem, can write only within that directory, and cannot access TCP or other Unix-domain sockets.
Sandboxed R sessions are unsupported on Linux and Windows.
Python, SQL, polling, interrupts, named sessions, bounded output, and runtime lifecycle tools are not implemented.

On macOS, `sandbox` launches the command under `/usr/bin/sandbox-exec`.
The command can read the host filesystem, can write regular files only in a dedicated temporary directory, and cannot access the network.
The policy also permits the device and IPC operations needed for supported R and Python workflows, including sandbox-created PTYs and Python multiprocessing semaphores.
This initial launcher waits only for the direct command.
Background descendants are unsupported: they may outlive the launcher, which attempts to remove their dedicated temporary directory on a best-effort basis when it returns.
Descendant supervision is intentionally deferred because it must account for process groups, session-detached children, signal forwarding, and PID reuse together.
Linux and Windows are not supported yet.

## Embedded Ark comparison snapshot

The following observations come from one local Apple Silicon macOS release-build run on July 29, 2026.
They are a comparison snapshot, not a general benchmark:

- Submitted functions retain synthetic MCP Console source URIs, and Ark tracebacks report the corresponding evaluation file, line, and column.
- `system("printf child-output")` returned `child-output`; Ark's stream capture includes direct subprocess stdout.
- A top-level task callback received Ark's internal `base::.ark_last_value` expression rather than the submitted expression.
- MCP process launch through initialization took about 10 ms.
  The first R evaluation took about 0.54 seconds.
  Twenty steady silent evaluations had a 1.4–1.8 ms median round trip across two runs.
- After the first evaluation there were two direct processes.
  The server used 5 threads and about 11,500 KiB RSS; the worker used 13 threads and about 106,300 KiB RSS, for about 117,800 KiB total RSS.
- The release binary was 28,885,040 bytes, about 27.55 MiB.
  There is no separate Ark executable, so this is also the combined installed binary footprint.
  A release build with an empty release target and warm Cargo and git caches took about 53 seconds.
- A simple plot produced one Jupyter `display_data` message containing `image/png`; the current MCP text adapter intentionally discards that MIME value and returns `[done]`.
- `View(mtcars)` in console mode opened a `positron.dataExplorer` comm without a Positron frontend.
  Ark and Amalthea expose generated, typed request and reply structures for schema, bounded values, filtering, sorting, profiles, and export, with extensive Ark tests.
  The comm has no compatibility negotiation and is generated from a Positron-owned schema, so it is not yet a stable MCP Console dependency boundary.
- Ark help uses a `positron.help` comm plus R and Ark HTTP servers on loopback.
  The current worker policy denies TCP, so `?mean` fails when R tries to bind its help server.
- The sandboxed worker is implemented and tested only on macOS.
  Linux and Windows use the unsupported worker launcher and reject R calls rather than starting R without a worker sandbox; the embedded dependency has not been tested on those platforms in this spike.

The proposed product and architecture remain under [`design-sketches/`](design-sketches/README.md).

## Development

Rust 1.94 or newer is required by the linked Ark crates.

The embedded-worker spike currently uses path dependencies from the local Ark checkout at `~/github/t-kalinowski/ark`.
It needs one Ark API addition: console-mode evaluation with `browser()` prompts routed through Jupyter stdin.
Before CI or packaging can build from a clean checkout, that change must be committed and the Ark and Amalthea dependencies must be replaced with one exact git revision.
After that pin, Cargo builds Ark into `mcp-console`; CI and packages do not install or ship a separate Ark executable.

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
