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
The server registers only a `send` tool and starts no runtime during MCP initialization or tool listing.
The tool accepts exactly one of an `r` string containing a complete R code cell or a `stdin` string supplying exact interactive input.
The first `r` call lazily starts the built-in R worker under the same sandbox policy as the `sandbox` command.

The hidden `worker` command takes ownership of the sideband, discovers `R_HOME` through the selected R executable inside the sandbox, and opens `R_HOME/lib/libR.dylib` by its absolute path.
It does not self-execute or set a dynamic-loader environment variable.
The command runs synchronously on the process main thread; only `serve` creates a Tokio runtime.

The worker embeds R through `libr` and `harp`.
It feeds each cell to R's DLL REPL, which parses and evaluates its expressions sequentially at top level in persistent global state and returns R console output, including every visible top-level value.
Cell EOF while R requires continuation input is a parse error, not an input prompt; earlier complete expressions from that cell remain applied.
R parse, evaluation, and auto-print failures are normal language outcomes with `isError: false`.
Silent successful evaluations return `[done]`.

`readline()` and `browser()` can suspend the active evaluation at `[input]`; later `stdin` calls buffer partial or multiple exact lines without adding a newline.
Unused buffered input is discarded when the evaluation ends.
New R code is rejected while input is required, and stdin is rejected at other times.

The hidden development option `serve --worker PATH` replaces the built-in worker with an executable that implements the same private protocol.
The Python fixture `tests/fixtures/zod` provides deterministic acceptance coverage for that boundary.
Worker startup, sandbox, process, and private-protocol failures are tool errors.
A stopped worker is not restarted implicitly.

The MCP process and worker communicate over a private inherited JSON-lines sideband so R output cannot corrupt MCP stdio.
On macOS, the worker runs under the existing Seatbelt policy with host reads allowed, regular-file writes limited to a private per-worker temporary directory, and network access denied.
Descendants inherit that policy.
R calls are unsupported on platforms without an implemented worker sandbox.
When MCP input closes, the server starts a one-second deadline and attempts graceful sideband shutdown without delaying it.
If the direct sandbox process is still running when time expires, the sandbox boundary force-stops its process group and reaps that direct process.

Native-worker descendants are not supervised or cleaned up after direct-worker termination and may outlive the server.
Top-level task callbacks receive the user's parsed expression.
Submitted R functions do not currently retain a source filename.
Direct subprocess output and output from forked descendants are unsupported.
Forked descendants cannot use the inherited sideband.
The version command prints the package name and version.
On macOS, the sandbox command launches a subprocess under `sandbox-exec` with host filesystem reads allowed, regular-file writes limited to a dedicated per-launch temporary directory, runtime device and IPC exceptions, and network access denied.
This initial launcher waits only for the direct command.
Background descendants are unsupported: they may outlive the launcher, which attempts to remove their dedicated temporary directory on a best-effort basis when it returns.
Descendant supervision is intentionally deferred because it must account for process groups, session-detached children, signal forwarding, and PID reuse together.
The sandbox command and worker are unsupported on Linux and Windows.
The session model, Python and SQL runtimes, polling, interrupts, explicit worker restart, the sidecar API, the viewer, environment management, output retention, and transcript generation do not exist yet.

## Product direction

MCP Console is intended to become a persistent, sandboxed R, Python, and DuckDB SQL console exposed through MCP.
The planned public MCP surface has two tools:

- `send` evaluates complete R, Python, or SQL cells, supplies interactive input to an active evaluation, and polls for output.
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
- `src/sideband.rs` — macOS inherited-pipe JSON-lines transport.
- `src/worker.rs` — embedded R initialization, evaluation, and console callbacks.
- `src/r_repl.c` — C-owned per-cell DLL-REPL iterator and long-jump boundary.
- `src/worker_client.rs` — server-side worker launch, lifecycle, and output collection.
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
- Run `scripts/check` before opening a PR.
