# AGENTS.md

Keep this file synchronized with the code that exists in the repository.
The documents under `design-sketches/` describe intended behavior, not implemented behavior.

## Current state

MCP Console is an initial Rust binary package.
The implemented commands are:

```text
mcp-console
mcp-console serve
mcp-console --version
mcp-console sandbox [--] COMMAND [ARG]...
```

The binary requires a subcommand.
The `serve` command runs an MCP server over stdio.
Clap provides command help, version output, argument parsing, and usage errors.
The server registers only a `send` tool and starts no runtime during MCP initialization or tool listing.
The tool accepts exactly one of an `r` string containing a complete R code cell or a `stdin` string supplying exact interactive input.
The first `r` call lazily starts one private embedded-R worker.
The worker parses the whole cell, evaluates its expressions sequentially at top level in persistent global state, and returns R console output, including every visible top-level value.
Incomplete source is a parse error, not a continuation prompt.
R parse, evaluation, and auto-print failures are normal language outcomes with `isError: false`.
Silent successful evaluations return `[done]`.
`readline()` and `browser()` can suspend the active evaluation at `[input]`; later `stdin` calls buffer partial or multiple exact lines without adding a newline.
Unused buffered input is discarded when the evaluation ends.
New R code is rejected while input is required, and stdin is rejected at other times.
Worker startup, sandbox, process, and private-protocol failures are tool errors.
Stopped workers are not restarted implicitly.
The MCP process and worker communicate over a private inherited JSON-lines sideband so R output cannot corrupt MCP stdio.
On macOS, the worker runs under the existing Seatbelt policy with host reads allowed, regular-file writes limited to a private per-worker temporary directory, and network access denied.
Descendants inherit that policy.
R calls are unsupported on platforms without an implemented worker sandbox.
MCP shutdown requests orderly direct-worker exit, waits one second, then terminates and reaps an unresponsive worker.
Native-worker descendants are not supervised or cleaned up after direct-worker termination and may outlive the server.
Top-level task callbacks receive an internal value-proxy expression rather than the user's parsed expression.
Submitted R functions do not currently retain a source filename.
Direct subprocess output and output from forked descendants are unsupported.
Forked descendants cannot use the inherited sideband.
The version command prints the package name and version.
On macOS, the sandbox command launches a subprocess under `sandbox-exec` with host filesystem reads allowed, regular-file writes limited to a dedicated per-launch temporary directory, runtime device and IPC exceptions, and network access denied.
This initial launcher waits only for the direct command.
Background descendants are unsupported: they may outlive the launcher, which attempts to remove their dedicated temporary directory on a best-effort basis when it returns.
Descendant supervision is intentionally deferred because it must account for process groups, session-detached children, signal forwarding, and PID reuse together.
The sandbox command is unsupported on Linux and Windows.
Python and SQL runtimes, polling, interrupts, named-session management, worker restart, the sidecar API, the viewer, environment management, output retention, and transcript generation do not exist yet.

## Product direction

MCP Console is intended to become a persistent, sandboxed R, Python, and DuckDB SQL console exposed through MCP.
The planned public MCP surface has two tools:

- `send` evaluates complete R, Python, or SQL cells, supplies interactive input to an active evaluation, and polls for output.
- `session` manages session requirements and lifecycle operations.

The MCP initialization identity remains `mcp-console`.
The intended default client registration name is `console`, for example `codex mcp add console -- mcp-console serve`.
Under Codex's current naming convention, the tools are `mcp__console.send` and `mcp__console.session`.
The initial runtime design uses R as the host, embeds Python through reticulate, and runs SQL through the DuckDB R package and DBI.
The eventual full-runtime worker backend remains an open design decision; the current embedded-R worker implements only this minimal console slice.

See `design-sketches/README.md` for the product overview and `design-sketches/docs/ARCHITECTURE.md` for the tentative architecture.

## Repository map

- `Cargo.toml` — Rust package metadata.
- `src/main.rs` — current binary entry point.
- `src/cli.rs` — clap command definitions and user-facing help.
- `src/server.rs` — MCP stdio server and R-evaluating `send` tool.
- `src/worker.rs` — private persistent embedded-R worker and supervisor client.
- `src/sideband.rs` — inherited Unix pipe transport for worker JSON-lines messages.
- `src/sandbox.rs` — platform dispatch for the sandbox process launcher.
- `src/sandbox/` — platform implementation and macOS Seatbelt policy.
- `tests/cli.rs` — public binary acceptance tests.
- `tests/transcripts/_run.py` — discovers transcript suites and compares case snapshots.
- `tests/transcripts/_support.py` — shared transcript types and MCP stdio client.
- `tests/transcripts/<suite>.py` — suites of named imperative transcript cases.
- `tests/transcripts/golden/SUITE/` — human-readable YAML 1.2 case transcripts.
- `tests/transcripts/README.md` — transcript test usage and authoring guide.
- `scripts/test` — builds the binary and runs selected external Python tests through `uv`.
- `scripts/format` — attempts each repository-wide formatter without requiring it.
- `scripts/check` — local formatting, Clippy, and test checks.
- `.github/workflows/ci.yaml` — formatting, Clippy, and test checks.
- `design-sketches/` — tentative product and architecture documents.
- `README.md` — current user-facing project status.
- `LICENSE` — project license.

Add modules only when implemented public behavior needs them.
Begin as one Cargo package and split crates only when a real boundary emerges.

## Working rules

- Keep PRs narrow and easy to review.
  Most PRs should stay under 200 lines of diff, counting additions and deletions.
  A larger PR is acceptable when splitting it would prevent each part from compiling, running, or being reviewed on its own.
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
