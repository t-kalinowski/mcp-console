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
On macOS, the hidden development option `serve --worker PATH` instead sends an `r` string to a worker launched through the same sandbox policy as the `sandbox` command.
When MCP input closes, the server starts a one-second deadline and attempts graceful sideband shutdown without delaying it.
If the direct worker sandbox process is still running when time expires, the sandbox boundary force-stops its process group and reaps that direct process.
The executable Python fixture `tests/fixtures/zod` implements the current ready/evaluate/output/completed/shutdown protocol for acceptance tests.
The version command prints the package name and version.
On macOS, the sandbox launch boundary passes only explicitly allowed file descriptors through exec.
The public `sandbox` command preserves stdin, stdout, and stderr; the development worker connects those streams to `/dev/null` and also passes its two owned sideband pipes.
The sandbox command runs under `sandbox-exec` with host filesystem opens allowed for reading, regular-file opens for writing limited to a dedicated per-launch temporary directory, runtime device and IPC exceptions, and network opens denied.
When it owns a terminal, the launcher runs the command in a dedicated foreground process group so terminal-generated signals are delivered once.
It relays `SIGHUP`, `SIGINT`, `SIGQUIT`, and `SIGTERM` sent directly to the launcher unless the signal was already blocked or ignored when the launcher started.
It imposes no signal timeout, so a command that handles or ignores a signal may continue running.
Before returning, it terminates descendants observed by the macOS process tracker, including `processx` children that create another session, and waits up to five seconds for them to be reaped.
On a process-observation error, the launcher attempts to terminate and reap the root process group before tearing down observed descendants.
If root termination cannot be confirmed, the launcher reports both failures and preserves its temporary directory instead of running teardown that assumes the root exited.
Detached descendants may remain when supervision itself fails because their identities can no longer be verified safely.
A descendant that orphans itself before macOS exposes it to the tracker is outside this initial supervision boundary.
The launcher does not proxy stopped and continued job-control states: `Ctrl-Z` and use as one stage of an interactive terminal pipeline are unsupported.
The development worker does not run the process tracker.
Forced shutdown kills and reaps its live root process group, but descendants may outlive it if the root exits first or they leave that group; full observed-descendant teardown applies only to the public `sandbox` command.
The sandbox command and development worker are unsupported on Linux and Windows.
The production worker, session model, language runtimes, sidecar API, viewer, environment management, output retention, and runtime transcript persistence do not exist yet.

## Product direction

MCP Console is intended to become a persistent, sandboxed R, Python, and DuckDB SQL console exposed through MCP.
The planned public MCP surface has two tools:

- `send` evaluates complete R, Python, or SQL cells, supplies interactive input to an active evaluation, and polls for output.
- `session` manages session requirements and lifecycle operations.

The MCP initialization identity remains `mcp-console`.
The intended default client registration name is `console`, for example `codex mcp add console -- mcp-console serve`.
Under Codex's current naming convention, the tools are `mcp__console.send` and `mcp__console.session`.
The initial runtime design uses R as the host, embeds Python through reticulate, and runs SQL through the DuckDB R package and DBI.
The production worker backend remains an open design decision.

See `design-sketches/README.md` for the product overview and `design-sketches/docs/ARCHITECTURE.md` for the tentative architecture.

## Repository map

- `Cargo.toml` — Rust package metadata.
- `src/main.rs` — current binary entry point.
- `src/cli.rs` — clap command definitions and user-facing help.
- `src/server.rs` — MCP stdio server, echoing `send` tool, and development-worker selection.
- `src/sideband.rs` — macOS server-side inherited-pipe JSON-lines transport.
- `src/worker_client.rs` — server-side worker launch, protocol, and output collection.
- `src/sandbox.rs` — reusable sandbox command builder and platform dispatch.
- `src/sandbox/macos.rs` — macOS sandbox launch and foreground-lifetime orchestration.
- `src/sandbox/macos/` — macOS descriptor, job-control, and process-tracking internals.
- `src/sandbox/read_only_policy.sbpl` — macOS Seatbelt policy.
- `src/sandbox/unsupported.rs` — unsupported-platform implementation.
- `tests/cli.rs` — unsupported-platform sandbox acceptance test.
- `tests/format_script.rs` — formatter-script acceptance tests.
- `tests/sandbox_policy.rs` — sandbox access-policy tests.
- `tests/sandbox_supervision.rs` — sandbox descendant-supervision tests.
- `tests/sandbox_terminal.rs` — sandbox terminal-behavior tests.
- `tests/fixtures/zod` — executable Python sideband worker used by acceptance tests.
- `tests/transcripts/_run.py` — discovers transcript suites and compares case snapshots.
- `tests/transcripts/_support.py` — shared transcript types and MCP stdio client.
- `tests/transcripts/<suite>.py` — suites of named imperative transcript cases.
- `tests/transcripts/golden/SUITE/` — human-readable YAML 1.2 case transcripts.
- `tests/transcripts/README.md` — transcript test usage and authoring guide.
- `scripts/test` — builds the binary and runs selected external Python tests through `uv`.
- `scripts/format` — attempts each repository-wide formatter without requiring it.
- `scripts/check` — local formatting, Clippy, and test checks.
- `.github/workflows/ci.yaml` — formatting, Clippy, and test checks.
- `docs/WORKER_PROTOCOL.md` — exact implemented development-worker launch and sideband protocol.
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
- Treat 400 lines as a prompt to split a file.
  Split earlier when it contains responsibilities that can be named independently; keep a cohesive implementation together when a split would make its control flow harder to follow.
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
