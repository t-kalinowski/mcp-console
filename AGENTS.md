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

The default command and `serve` run an MCP server over stdio.
The server registers only a `console` tool, which accepts any JSON object and echoes it as JSON text.
The version command prints the package name and version.
On macOS, the sandbox command launches a subprocess under `sandbox-exec` with host filesystem reads allowed, regular-file writes limited to a dedicated per-launch temporary directory, runtime device and IPC exceptions, and network access denied.
When it owns a terminal, the launcher runs the command in a dedicated foreground process group so terminal-generated signals are delivered once.
It relays `SIGHUP`, `SIGINT`, `SIGQUIT`, and `SIGTERM` sent directly to the launcher unless the signal was already blocked or ignored when the launcher started.
It imposes no signal timeout, so a command that handles or ignores a signal may continue running.
Before returning, it terminates descendants observed by the macOS process tracker, including `processx` children that create another session.
A process-observation error terminates the root process group, reports an error, and preserves its temporary directory instead of treating the process as exited.
Detached descendants may remain when supervision itself fails because their identities can no longer be verified safely.
A descendant that orphans itself before macOS exposes it to the tracker is outside this initial supervision boundary.
The launcher does not proxy stopped and continued job-control states: `Ctrl-Z` and use as one stage of an interactive terminal pipeline are unsupported.
The sandbox command is unsupported on Linux and Windows.
The session model, language runtimes, sidecar API, viewer, environment management, output retention, and transcript generation do not exist yet.

## Product direction

MCP Console is intended to become a persistent, sandboxed R, Python, and DuckDB SQL workbench exposed through MCP.
The planned public MCP surface has two tools:

- `console` evaluates complete R, Python, or SQL cells, supplies interactive input to an active evaluation, and polls for output.
- `console_session` manages session requirements and lifecycle operations.

The initial runtime design uses R as the host, embeds Python through reticulate, and runs SQL through the DuckDB R package and DBI.
The worker backend remains an open design decision.

See `design-sketches/README.md` for the product overview and `design-sketches/docs/ARCHITECTURE.md` for the tentative architecture.

## Repository map

- `Cargo.toml` — Rust package metadata.
- `src/main.rs` — current binary entry point.
- `src/server.rs` — MCP stdio server and echoing `console` tool.
- `src/sandbox.rs` — platform dispatch for the sandbox process launcher.
- `src/sandbox/` — platform implementation and macOS Seatbelt policy.
- `tests/cli.rs` — public binary acceptance tests.
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
