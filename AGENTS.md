# AGENTS.md

Keep this file synchronized with the code that exists in the repository.
The documents under `design-sketches/` describe intended behavior, not implemented behavior.

## Current state

MCP Console is an initial Rust binary package.
The implemented commands are:

```text
mcp-console serve
mcp-console --help
mcp-console help [COMMAND]
mcp-console --version
mcp-console sandbox [--] COMMAND [ARG]...
```

The binary requires a subcommand.
The `serve` command runs an MCP server over stdio.
Clap provides command help, version output, argument parsing, and usage errors.
The server registers only a `send` tool.
Supplying `r` starts one complete cell and waits for up to `timeout_ms`, which defaults to 60 seconds.
If that wait expires, `send` returns the newline-prefixed banner `\n[running]` without stopping the computation; a later call without `r` polls it, and a poll while idle returns `\n[idle]`.
Concurrent `send` calls are unsupported.
Supplying `stdin` with `r`, during an evaluation, or while idle queues exact UTF-8 bytes to worker fd 0 without adding a newline, inspecting or limiting the text, or waiting for an input request.
A nonempty idle stdin call lazily starts the worker when needed, queues the bytes, and returns `\n[idle]`; `timeout_ms` does not bound that startup because the call does not wait on an evaluation.
Payload end is not EOF; the R console callback reads through one newline or its supplied buffer, and unread bytes may satisfy later console or direct reads, including in a later evaluation.
An `input_requested` frame is provisional for up to 10 milliseconds; a matching `input_received` after a successful console read suppresses the `\n[input]` banner, while an unmatched request returns it after that grace or at the MCP deadline, whichever comes first.
The receipt describes that runtime read, not a submitted payload or byte count, and direct fd-0 reads emit neither frame.
New code is rejected until the running evaluation's result has been collected.
On macOS, the first nonempty stdin submission or evaluation lazily starts the built-in R worker under the same sandbox policy as the `sandbox` command.
The worker embeds R through `libr` and `harp`, retains global state, and feeds each complete cell through R's DLL REPL iterator.
R parses and evaluates its expressions sequentially, captures console output, prints visible values, and performs native top-level bookkeeping.
Cell EOF while R requires continuation input is an error; earlier complete expressions from that cell remain applied.
R parse, evaluation, and auto-print failures are normal language outcomes with `isError: false`; silent successful cells with no pending stream output return `[done]`.
Submitted R functions do not currently retain a source filename.
Worker standard output and standard error are piped and collected continuously, including while the worker is idle.
Each pipe reader queues raw byte chunks, and each `send` response decodes and drains complete UTF-8 prefixes from bytes already collected at its response boundary; later bytes remain for the next response.
Idle, running, and input responses append the literal `\n[idle]`, `\n[running]`, or `\n[input]` banner; its leading newline is present even when no output precedes it.
Completion returns collected stream and sideband output instead of `[done]` when either produced text.
Ordering between the two standard streams and sideband output is best effort; incomplete UTF-8 remains with its pipe until a later response, and invalid UTF-8 is replaced when output is rendered.
The built-in R worker and custom workers send console prompt fields verbatim; the server appends a prompt before its `\n[input]` banner without trimming it.
Output from descendants that inherit standard output or standard error follows the same path, but this does not add descendant supervision; forked descendants cannot use the inherited sideband.
The hidden `worker` command takes ownership of the sideband, discovers `R_HOME` through the selected R executable inside the sandbox, and opens `R_HOME/lib/libR.dylib` by its absolute path.
It does not self-execute or set a dynamic-loader environment variable.
The worker command runs synchronously on the process main thread; only `serve` creates a Tokio runtime.
The hidden development option `serve --worker PATH` replaces the built-in worker with an executable that implements the same sideband request/receipt protocol and fd-0 input contract.
The Python fixture `tests/fixtures/zod` provides deterministic acceptance coverage for that boundary, direct fd-0 input, captured standard streams, and server-owned timeout and polling mechanics.
An infrastructure or protocol failure is returned as a tool error, force-stops and discards that worker, and lets the next evaluation start a fresh worker.
When MCP input closes, the server starts a one-second deadline and attempts graceful sideband shutdown without delaying it.
If the direct sandbox process is still running when time expires, the sandbox boundary force-stops its process group and reaps that direct process.
The version command prints the package name and version.
On macOS, the sandbox command launches a subprocess under `sandbox-exec` with host filesystem reads allowed, regular-file writes limited to a dedicated per-launch temporary directory, runtime device and IPC exceptions, and network access denied.
This initial launcher waits only for the direct command.
Background descendants are unsupported: they may outlive the launcher, which attempts to remove their dedicated temporary directory on a best-effort basis when it returns.
Descendant supervision is intentionally deferred because it must account for process groups, session-detached children, signal forwarding, and PID reuse together.
The sandbox command and worker are unsupported on Linux and Windows.
The session model, Python and SQL runtimes, sidecar API, viewer, environment management, output retention, and transcript generation do not exist yet.

## Product direction

MCP Console is intended to become a persistent, sandboxed R, Python, and DuckDB SQL console exposed through MCP.
The planned public MCP surface has two tools:

- `send` evaluates complete R, Python, or SQL cells, writes to the session's stdin stream, and polls for output.
- `session` manages session requirements and lifecycle operations.

The MCP initialization identity remains `mcp-console`.
The intended default client registration name is `console`, for example `codex mcp add console -- mcp-console serve`.
Under Codex's current naming convention, the tools are `mcp__console.send` and `mcp__console.session`.
The implemented R slice embeds R through `libr` and `harp`.
The planned runtime uses R as the host, embeds Python through reticulate, and runs SQL through the DuckDB R package and DBI.
The backend for that broader runtime surface remains an open design decision.

See `design-sketches/README.md` for the product overview and `design-sketches/docs/ARCHITECTURE.md` for the tentative architecture.

## Repository map

- `Cargo.toml` — Rust package metadata.
- `build.rs` — macOS C-shim build.
- `src/main.rs` — current binary entry point.
- `src/cli.rs` — clap command definitions and user-facing help.
- `src/server.rs` — MCP stdio server, `send` tool, and worker selection.
- `src/r_repl.c` — C-owned per-cell DLL-REPL iterator and long-jump boundary.
- `src/sideband.rs` — macOS inherited-pipe JSON-lines transport.
- `src/worker.rs` — embedded R initialization, evaluation, and console callbacks.
- `src/worker_client.rs` — server-side worker launch, lifecycle, fd-0 input, and output collection.
- `src/worker_protocol.rs` — shared sideband message definitions.
- `src/sandbox.rs` — platform dispatch for the sandbox process launcher.
- `src/sandbox/` — platform implementation and macOS Seatbelt policy.
- `tests/cli.rs` — public binary acceptance tests.
- `tests/fixtures/zod` — executable Python sideband worker used by acceptance tests.
- `tests/transcripts/r.py` — public built-in R worker acceptance suite.
- `tests/transcripts/_run.py` — discovers transcript suites and compares case snapshots.
- `tests/transcripts/_support.py` — shared transcript types and MCP stdio client.
- `tests/transcripts/<suite>.py` — suites of named imperative transcript cases.
- `tests/transcripts/golden/SUITE/` — human-readable YAML 1.2 case transcripts.
- `tests/transcripts/README.md` — transcript test usage and authoring guide.
- `scripts/test` — builds the binary and runs selected external Python tests through `uv`.
- `scripts/format` — attempts each repository-wide formatter without requiring it.
- `scripts/check` — local formatting, Clippy, and test checks.
- `.github/workflows/ci.yaml` — formatting, Clippy, and test checks.
- `docs/WORKER_PROTOCOL.md` — exact implemented worker launch and sideband protocol.
- `design-sketches/` — tentative product and architecture documents.
- `README.md` — current user-facing project status.
- `LICENSE` — project license.

Add modules only when implemented public behavior needs them.
Begin as one Cargo package and split crates only when a real boundary emerges.

## Working rules

- Keep PRs coherent, compact, and easy to review.
  As a heuristic, aim to keep implementation-code changes under 200 added and deleted lines.
  Tests, golden snapshots, and documentation do not count toward this guideline.
  The line count is not a limit; prefer a larger coherent change over splits that make the work harder to understand or validate.
- Each PR should implement and test one observable behavior.
  Update design documents in the same PR only when they describe that behavior.
- Add a public acceptance or regression test first and confirm that it fails before implementing behavior.
- Test through public interfaces.
  Do not add tests for private helpers.
- Format embedded R, Python, SQL, and shell test programs as multiline raw strings.
  Use escape sequences such as `\n` only when the program needs that character as data, not to lay out its source.
- Keep complete code cells separate from interactive `stdin`.
- Keep the MCP adapter independent of interpreter implementation details.
- Treat all runtime execution as shell-class capability and place safety at the worker-process boundary.
- Update this file when a PR changes the implemented surface or repository map.
- Before every commit, run `scripts/format` and review its changes.
- Run `scripts/check` before opening a PR.
