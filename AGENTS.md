# AGENTS.md

This file contains repository-wide instructions and a navigation map.
Keep it synchronized with implemented code.
Detailed runtime behavior belongs in the protocol documents, source, and public transcript tests.

The documents under `design-sketches/` describe intended behavior, not the current implementation.

## Sources of truth

- `docs/WORKER_PROTOCOL.md` describes the implemented worker launch, sideband, language adapters, evaluation, input, output, and lifecycle contract.
- `docs/RELAY_PROTOCOL.md` describes the private server-to-relay transport and sandbox process boundary.
- `docs/TOOL_DESCRIPTIONS.md` contains the exact registered MCP tool and property descriptions.
- `tests/transcripts/README.md` describes transcript boundaries, selectors, normalization, and snapshot updates.
- `README.md` describes the current user-facing project status.

When documentation and code disagree, inspect the implementation and public tests before changing either.
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

The transcript runner's submitted, started, and final status lines are runner user-interface output only.
They are not MCP, relay, sideband, or worker-stream records.

## Process and ownership boundaries

MCP Console has three process boundaries:

1. The client and server communicate through MCP JSON-RPC over stdio.
2. The server and one per-generation relay communicate through the private, ordered JSONL protocol in `docs/RELAY_PROTOCOL.md`.
3. The relay and worker communicate through the worker sideband plus worker fd 0, 1, and 2 as documented in `docs/WORKER_PROTOCOL.md`.

Keep these ownership rules intact:

- The relay is a thin ordered transport and worker supervisor.
  It owns local worker pipes, sideband translation, signal delivery, bounded termination, and reaping.
  It preserves each producer's order and supplies serialized observation order; it does not reconstruct chronology across independent sideband, stdout, and stderr pipes.
- The server owns worker-generation state, operation admission, output cuts, pending-output budgets, response assembly, delivery ownership, retained requirements, and host resolvers.
  Do not move these responsibilities into the relay.
- Restart, replacement, evaluation admission, stdin writes, resolver callbacks, and retained-environment commits are scoped to the worker generation that accepted them.
  Work admitted for an old generation must not reach its replacement.
- R, Python, and DuckDB dependency resolution runs outside the worker sandbox.
  Accept only documented trusted inputs: IR package references with `IR_NO_LOCAL_SOURCES`, named PEP 508 registry requirements under the trusted startup resolver configuration, and validated DuckDB extension names.
  Accepted installation or build code may execute with server permissions.
- Treat submitted R, Python, and SQL as shell-class capability and enforce isolation at the worker-process boundary.
  Keep complete code cells separate from interactive `stdin`, and keep the MCP adapter independent of interpreter implementation details.
- Production R and Python programs under `src/` are included in the binary at compile time.
  The worker and resolvers must not load them from the source tree or installation layout at runtime.

## Repository map

### Public interface and records

- `src/main.rs`, `src/cli.rs` — binary entry point and command definitions.
- `src/server.rs`, `src/server_transport.rs` — MCP tools, stdio transport, and response-delivery ownership.
- `src/transcript.rs` — append-only tool journal and image artifacts.

### Protocols, relay, and worker orchestration

- `src/worker_protocol.rs`, `src/sideband.rs` — relay-worker message and framing contract.
- `src/relay_protocol.rs` — server-relay JSONL message and framing contract.
- `src/worker_relay.rs` — sandboxed worker launch, I/O forwarding, signaling, shutdown, and reaping.
- `src/worker_client.rs`, `src/worker_client/` — server-owned environment, evaluation, lifecycle, ordered event dispatch, output tape, and macOS relay transport.
- `src/worker.rs`, `src/r_repl.c` — embedded-R worker, cell dispatch, console callbacks, and the C-owned DLL-REPL boundary.

### Language adapters

- `src/r_bridge.rs` — shared Rust FFI for process-lifetime private R bridge environments.
- `src/python.rs`, `src/python/bridge.R`, `src/python/runtime.py` — reticulate orchestration, R bridge, and Python evaluator runtime.
- `src/sql.rs`, `src/sql/bridge.R` — persistent DuckDB/DBI orchestration and R bridge.
- `src/r_graphics.rs`, `src/r_graphics.c`, `src/r_graphics/bridge.R` — managed graphics orchestration, C callback boundary, and R bridge.
- `src/r_environment.rs`, `src/r_environment/bridge.R` — live R-library bridge.

### Resolvers and sandbox

- `src/resolver.rs`, `src/resolver/` — retained host environments, validation, platform implementations, and resolver process-group lifecycle.
- `src/resolver/programs/` — compile-time R programs for managed Python, Python-version selection, DuckDB extensions, and R-library discovery.
- `src/sandbox.rs`, `src/sandbox/` — platform dispatch and macOS Seatbelt policy.

### Tests and development scripts

- `tests/cli.rs` — public CLI acceptance tests.
- `tests/fixtures/` — deterministic workers, relays, resolvers, and package fixtures.
- `tests/transcripts/client_server/` — public MCP client-server behavior.
- `tests/transcripts/server_relay/` — private server-relay wire behavior.
- `tests/transcripts/relay_worker/` — worker sideband and standard-stream behavior through the relay.
- `tests/transcripts/cli/` — direct CLI transcripts.
- `tests/transcripts/golden/` — generated YAML 1.2 snapshots.
- `scripts/test` — binary build and selected transcript execution.
- `scripts/validate_runtime_sources.py` — extracted R/Python inventory and syntax validation.
- `scripts/format`, `scripts/check` — repository-wide formatting and checks.

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
