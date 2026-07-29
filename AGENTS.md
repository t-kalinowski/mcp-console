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
The server registers only a `send` tool, which accepts any JSON object and echoes it as JSON text.
The version command prints the package name and version.
On macOS, the sandbox command launches a subprocess under `sandbox-exec` with host filesystem reads allowed, regular-file writes limited to a dedicated per-launch temporary directory, runtime device and IPC exceptions, and network access denied.
This initial launcher waits only for the direct command.
Background descendants are unsupported: they may outlive the launcher, which attempts to remove their dedicated temporary directory on a best-effort basis when it returns.
Descendant supervision is intentionally deferred because it must account for process groups, session-detached children, signal forwarding, and PID reuse together.
The sandbox command is unsupported on Linux and Windows.
The session model, language runtimes, sidecar API, viewer, environment management, output retention, and transcript generation do not exist yet.

## Product direction

MCP Console is intended to become a persistent, sandboxed R, Python, and DuckDB SQL console exposed through MCP.
The planned public MCP surface has two tools:

- `send` evaluates complete R, Python, or SQL cells, supplies interactive input to an active evaluation, and polls for output.
- `session` manages session requirements and lifecycle operations.

The MCP initialization identity remains `mcp-console`.
The intended default client registration name is `console`, for example `codex mcp add console -- mcp-console serve`.
Under Codex's current naming convention, the tools are `mcp__console.send` and `mcp__console.session`.
The initial runtime design uses R as the host, embeds Python through reticulate, and runs SQL through the DuckDB R package and DBI.
The worker backend remains an open design decision.

See `design-sketches/README.md` for the product overview and `design-sketches/docs/ARCHITECTURE.md` for the tentative architecture.

## Repository map

- `Cargo.toml` — Rust package metadata.
- `src/main.rs` — current binary entry point.
- `src/cli.rs` — clap command definitions and user-facing help.
- `src/server.rs` — MCP stdio server and echoing `send` tool.
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
