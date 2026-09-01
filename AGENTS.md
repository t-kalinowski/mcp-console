# AGENTS.md

This file contains repository-wide instructions and a navigation map.
Keep it synchronized with implemented code.
Detailed current behavior belongs in the documents indexed by `docs/README.md`, source, and public transcript tests.

The documents under `design-sketches/` describe intended behavior, not the current implementation.

## Sources of truth

- `README.md` describes the current user-facing project status.
- `docs/README.md` maps the implemented documentation by audience.
- `docs/ARCHITECTURE.md` describes the implemented process structure, ownership, and lifecycle.
- `docs/SANDBOX_SUPERVISION.md` describes macOS normal and crash-independent sandbox lifetime supervision.
- `docs/BUILTIN_RUNTIME.md` describes user-visible behavior of the built-in mixed-language console.
- `docs/REQUIREMENTS.md` describes dependency and environment behavior and its trust boundary.
- `docs/WORKER_PROTOCOL.md` defines the exact relay-worker and custom-worker contract.
- `docs/RELAY_PROTOCOL.md` defines the exact private server-relay transport.
- `docs/TOOL_DESCRIPTIONS.md` is a human-readable mirror of registered MCP tool and property prose.
  The actual `tools/list` result and the registered strings and Rust doc comments in `src/server.rs` are authoritative.
- `tests/transcripts/README.md` describes transcript boundaries, selectors, normalization, and snapshot updates.
- `design-sketches/` contains intended or exploratory future design only.

When documentation and code disagree, source and public acceptance tests are the final authority.
Do not treat `design-sketches/` as evidence of implemented behavior.

## Platform and development

The sandbox command, worker relay, and built-in worker are supported on macOS.
Linux and Windows are not supported yet.
CI runs the complete check on macOS.

Run commands from the repository root:

```text
scripts/format
scripts/check
scripts/test [BOUNDARY/SUITE[::CASE]]
scripts/test --list
scripts/test --update BOUNDARY/SUITE[::CASE]
```

`scripts/format` attempts Ruff, Yamark, rustfmt, and Air in sequence.
A missing or failing formatter does not prevent the remaining formatters from running or make the script fail, so review its output and resulting changes.
`scripts/check` validates extracted runtime sources, checks Rust formatting and Clippy, runs Rust tests, and runs the complete transcript suite.

### Transcript goldens

Never hand-edit files under `tests/transcripts/golden/`.
They may change only through `scripts/test --update ...` or Yamark via `scripts/format`.
If regeneration produces an incorrect snapshot, fix the code or serializer and regenerate it.

The transcript runner's progress dots and slow or failed case status lines are runner user-interface output only.
They are not MCP, relay, sideband, or worker-stream records.

## Process and ownership boundaries

MCP Console has four process boundaries:

1. The client and server communicate through MCP JSON-RPC over stdio.
2. Each host sandbox owner initializes one manager over a private inherited Unix socket, receives its readiness and ownership-commit responses, and uses the same stream for bounded retirement.
   The server starts one manager for each worker generation, which may evaluate multiple cells before restart or replacement.
   The standalone launcher starts one manager for each invocation of `mcp-console sandbox`, which runs one direct child command, and uses the stream for its final directory disposition.
3. The server and one per-generation relay communicate through the private, ordered JSONL protocol in `docs/RELAY_PROTOCOL.md`.
4. The relay and worker communicate through the worker sideband plus worker fd 0, 1, and 2 as documented in `docs/WORKER_PROTOCOL.md`.

Keep these ownership rules intact:

- The relay is a thin ordered transport and worker supervisor.
  It owns local worker transports, sideband translation, and direct-worker signal delivery, bounded termination, and reaping.
  It preserves each producer's order and supplies serialized observation order; it does not reconstruct chronology across independent sideband, stdout, and stderr transports.
- The server owns host-side relay lifetime orchestration and retirement, worker-generation state, operation admission, output cuts, pending-output budgets, response assembly, delivery ownership, retained requirements, and host resolvers.
  It releases each relay root's private startup gate only after manager-failure recovery is installed and manager ownership is committed.
  Do not move these responsibilities into the relay.
- Treat each relay and its worker process tree, and each standalone command tree, as one sandboxed lifetime.
  A host-side sandbox manager owns primary observed-descendant tracking, bounded force termination, and private-directory cleanup for that lifetime.
  Its host owner retains a backup directory guard and takes over bounded cleanup if the manager exits unsuccessfully while the sandbox root remains live and pinned.
  That fallback can reconstruct only descendants still reachable from the root's current ancestry.
- The standalone launcher owns the direct command's exit status, foreground-terminal transfer, signal relaying, and final temporary-directory disposition.
  It releases the command's private startup gate only after manager ownership is committed, and retains the direct root waitably through manager cleanup.
- The sandbox manager does not own logical generation state, command exit status, relay transport, or terminal semantics.
- Restart, replacement, evaluation admission, stdin writes, resolver callbacks, and retained-environment commits are scoped to the worker generation that accepted them.
  Work admitted for an old generation must not reach its replacement.
- R, Python, and DuckDB dependency resolution runs outside the worker sandbox.
  Accept only documented trusted inputs: `ir` package references with `IR_NO_LOCAL_SOURCES`, named PEP 508 registry requirements under the trusted startup resolver configuration, and validated DuckDB extension names.
  Accepted installation or build code may execute with server permissions.
- Treat submitted R, Python, and SQL as shell-class capability and enforce isolation at the worker-process boundary.
  Keep complete code cells separate from interactive `stdin`, and keep the MCP adapter independent of interpreter implementation details.
- Production R and Python programs under `src/` are included in the binary at compile time.
  The worker and resolvers must not load them from the source tree or installation layout at runtime.

## Repository map

### Public interface and records

- `src/main.rs`, `src/cli.rs` — binary entry point and command definitions.
- `src/server.rs`, `src/server_transport.rs` — MCP tools, stdio transport, and response-delivery ownership.
- `src/transcript.rs`, `src/transcript/markdown.rs` — append-only tool journal, Markdown and source-only Quarto projections, and image artifacts.
- `r/` — thin ellmer package that resolves and manages `mcp-console serve` as a persistent tool.

### Protocols, relay, and worker orchestration

- `src/worker_protocol.rs`, `src/sideband.rs` — relay-worker message and framing contract.
- `src/relay_protocol.rs` — server-relay JSONL message and framing contract.
- `src/worker_relay.rs` — sandboxed worker launch, I/O forwarding, signaling, shutdown, and reaping.
- `src/worker_client.rs`, `src/worker_client/` — server-owned environment, evaluation, lifecycle, ordered event dispatch, output tape, and macOS relay transport.
- `src/sandbox.rs`, `src/sandbox/{child,command,spawn}.rs`, `src/sandbox/supervision.rs`, `src/sandbox/supervision/` — sandbox command construction, launch, child retirement, primary host-manager supervision, owner-side manager-failure recovery, and standalone job control.
- `src/worker.rs`, `src/worker/embedded_r.rs`, `src/r_repl.c` — worker-facing facade, current embedded-R backend, cell dispatch, console callbacks, and the C-owned DLL-REPL boundary.

### Language adapters

- `src/r_bridge.rs` — shared Rust FFI for process-lifetime private R bridge environments.
- `src/python.rs`, `src/python/library.rs`, `src/python/reticulate.rs`, `src/python/initialize.R`, `src/python/bridge.R`, `src/python/runtime.py` — Rust-owned Python runtime facade, CPython initialization, current reticulate backend, R bridges, and Python evaluator runtime.
- `src/sql.rs`, `src/sql/r_dbi.rs`, `src/sql/py_dbapi.rs`, `src/sql/bridge.R`, `src/sql/dbapi.py` — worker-facing SQL router, R DBI and Python DB-API providers, and their runtime bridges.
- `src/r_graphics.rs`, `src/r_graphics.c`, `src/r_graphics/bridge.R` — managed graphics orchestration, C callback boundary, and R bridge.
- `src/r_environment.rs`, `src/r_environment/bridge.R` — live R-library bridge.

### Resolvers and sandbox

- `src/resolver.rs`, `src/resolver/` — retained host environments, direct Python-version selection, validation, platform implementations, and resolver process-group lifecycle.
- `src/resolver/programs/` — compile-time R programs for DuckDB extension preparation, R-library resolution, and `uv` discovery.
- `src/sandbox/macos.rs`, `src/sandbox/file_descriptors.rs` — macOS Seatbelt policy and inherited-descriptor boundary.

### Tests and development scripts

- `tests/cli.rs` — public CLI and narrow OS/process-lifecycle acceptance tests that cannot be expressed at the MCP boundary.
- `tests/fixtures/` — deterministic workers, relays, resolvers, and package fixtures.
- `tests/transcripts/client_server/` — public MCP client-server behavior.
- `tests/transcripts/server_relay/` — private server-relay wire behavior.
- `tests/transcripts/relay_worker/` — worker sideband and standard-stream behavior through the relay.
- `tests/transcripts/cli/` — direct CLI transcripts.
- `tests/transcripts/golden/` — generated YAML 1.2 snapshots.
- `r/tests/testthat/` — R package protocol and ellmer adapter tests.
- `scripts/release.py`, `tests/release.py` — release validation and installed-wheel acceptance.
- `scripts/test` — binary build and selected transcript execution.
- `scripts/validate_runtime_sources.py` — extracted R/Python inventory and syntax validation.
- `scripts/format`, `scripts/check-core`, `scripts/check` — formatting, core checks, and repository-wide checks.

## Working rules

- Keep PRs coherent and easy to review.
  For behavior-changing implementation, aim for fewer than 200 added and deleted lines as a heuristic.
  Mechanical moves, internal-only reorganization, tests, goldens, and documentation do not count toward it.
  Prefer a larger coherent change over an artificial split.
- Keep each behavior-changing PR to one observable behavior.
  Internal-only refactors may stand alone but must preserve observable behavior.
- For a public behavior change, first add a public acceptance or regression test and confirm it fails.
  Verify an internal-only refactor with the existing public suite.
  Test public interfaces, not private helpers.
- For internal coordination and lifecycle control, prefer blocking event-driven waits with an explicit wakeup path.
  Do not use busy loops or short fixed-interval polling when the state transition can notify a condition variable, descriptor, or platform event.
- Preserve client-visible runtime output in transcript snapshots, including complete errors and tracebacks.
  Normalize only incidental values such as run-specific temporary paths; do not replace behavior with summaries or placeholders.
- Keep embedded R, Python, SQL, and shell fixture programs as readable multiline strings.
  Use escapes such as `\n` only when the character is data.
- Refactor internal modules when the implemented responsibilities have a clearer boundary.
  Do not add structure for planned behavior.
  Treat roughly 500 lines of production source as a prompt to reassess a file, not a hard limit, and keep one Cargo package until the implementation presents a concrete crate boundary.
- Update design documents in the same PR only when they describe changed behavior.
  Update this file when repository-wide constraints or navigation change.
- Run `scripts/format` and review its changes before every commit.
  Run `scripts/check` before opening a PR.
