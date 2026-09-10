# AGENTS.md

This file contains repository-wide instructions and a navigation map.
Keep it synchronized with implemented code.
Detailed current behavior belongs in the documents indexed by `docs/README.md`, source, and public transcript tests.

The documents under `design-sketches/` describe intended behavior, not the current implementation.

## Sources of truth

- `README.md` describes the current user-facing project status.
- `RELEASE.md` defines release preparation, wheel rehearsal, publication, verification, and recovery.
- `docs/README.md` maps the implemented documentation by audience.
- `docs/ARCHITECTURE.md` describes the implemented process structure, ownership, and lifecycle.
- `docs/SANDBOX.md` describes application policy, runner integration, supported hosts, and lifetime guarantees.
- `docs/SANDBOX_CONFIGURATION.md` defines the public configuration interface, environment ownership, caller examples, and transport integrity.
- `docs/LINUX_COMPATIBILITY.md` records capability requirements, security comparisons, native backend differences, and tested Linux baselines.
- `docs/SANDBOX_RUNNER_INTEGRATION.md` records the migration baseline, fixture changes, supported-host validation, and changed guarantees.
- `docs/BUILTIN_RUNTIME.md` describes user-visible behavior of the built-in mixed-language console.
- `docs/SEND_OPERATIONS.md` defines validation, preparation, control, input, and timeout ordering for `send`.
- `docs/REQUIREMENTS.md` describes dependency and environment behavior and its trust boundary.
- `docs/WORKER_PROTOCOL.md` defines the exact relay-worker and custom-worker contract.
- `docs/RELAY_PROTOCOL.md` defines the exact private server-relay transport.
- `docs/TOOL_DESCRIPTIONS.md` gives editorial guidance for registered MCP tool and property prose and links to the canonical handshake snapshot.
  The actual `tools/list` result and the registered strings and Rust doc comments in `src/server.rs` are authoritative.
- `tests/boundaries/README.md` describes process boundaries, selectors, normalization, and snapshot updates.
- `design-sketches/` contains intended or exploratory future design only.

When documentation and code disagree, source and public acceptance tests are the final authority.
Do not treat `design-sketches/` as evidence of implemented behavior.

## Platform and development

The worker relay, built-in worker, and managed resolvers support macOS and Linux.
The default sandbox and standalone sandbox command support both platforms.
Linux requires procfs, permitted native namespace operations, and the selected policy enforcement capabilities; see `docs/LINUX_COMPATIBILITY.md` for tested baselines and constrained-host behavior.
Do not infer support from a kernel version alone.
Windows is not supported.
Other Unix operating systems are not supported build or runtime targets; shared `cfg(unix)` modules do not imply support for them.
Retain platform conditionals for modules that use OS-specific APIs and for selecting different implementations or unsupported-platform stubs; avoid redundant gates on shared code.
CI runs core checks and all capability-applicable transcript modes on macOS and Linux.
Keep one CI job per platform.
CI restores Cargo build data across source and dependency changes within the same native build environment and UTC week, with incremental compilation enabled.
Keep the intentional weekly build-cache reset.
Ordinary source edits reuse one cached baseline per dependency set rather than saving another target-directory snapshot.
Keep `main` caches reusable by PRs and remove caches for closed PRs.
Cargo determines which crates need rebuilding.
An exact match of compiled and packaging inputs additionally lets CI skip the release build and reuse a finished wheel and native bundle; it still runs the current tests.
Bump `CI_BUILD_CACHE_VERSION` in `.github/workflows/ci.yaml` when build inputs outside the hashed files change, such as workflow build flags or native dependency setup; unrelated workflow edits must not invalidate build caches.
Source installation checks run after the other checks because they replace and hide the shared Cargo target directory.
Python package builds and installations require Python 3.11 or later.

macOS and Linux uv source installations prepare the pinned sandbox companion before invoking the application's Cargo build, using a dedicated checkout under `target`.
The pinned checkout's `codex-rs/rust-toolchain.toml` owns the runner's compiler configuration; Console's toolchain selection is independent.
Direct Cargo or Maturin builds require `scripts/stage-sandbox-runner` first; `scripts/check` performs this preparation.
Install development checkouts with `uv tool install --reinstall .`; bare `cargo install` does not install the companion bundle.
Build reuse follows Cargo's tracked inputs; external tool changes through `PATH` can require cleaning the affected Cargo build directories, as described in `RELEASE.md`.
Native Cargo bundles require the default shared build/target layout; a separate intermediate build directory is unsupported for running the Cargo output.
The Python packaging backend holds a checkout-local lock from staging through wheel creation.
Direct staging, Cargo, and Maturin commands require exclusive use of their source checkout.
See `RELEASE.md` for prerequisites, bundle layout, build caches, and the explicit source-checkout override.
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
`scripts/check` validates extracted runtime sources, checks Rust formatting and Clippy, runs Rust tests in debug, runs the complete transcript suite against the release executable, and checks uv source and wheel installations with a shared Cargo target directory.

### Boundary snapshots

Cases run by default; declare capability requirements beside affected cases with `@requires(...)` from `tests/support/requirements.py`.
Keep platform availability in test support.
Use `@executions(DIRECT, SANDBOXED)` and `execution.serve(...)` to reuse ordinary cases across applicable execution modes with a shared snapshot.
Sandbox contracts use explicit sandbox fixtures and requirements.

Never hand-edit files under `tests/snapshots/`.
They may change only through `scripts/test --update ...` or Yamark via `scripts/format`.
If regeneration produces an incorrect snapshot, fix the code or serializer and regenerate it.

The transcript runner's progress dots and slow or failed case status lines are runner user-interface output only.
They are not MCP, relay, sideband, or worker-stream records.

## Process and ownership boundaries

The suite covers client-server MCP, server-relay JSONL, relay-worker sideband and standard streams, and the public CLI, including sandbox supervision.
`docs/ARCHITECTURE.md` owns component contracts; `docs/SANDBOX.md` owns the Console policy and external runner boundary.
Keep these invariants intact:

- The server owns logical relay lifetime orchestration and retirement, worker-generation state, operation admission, output cuts, pending-output budgets, response assembly, delivery ownership, retained requirements, and host resolvers.
  By default, it starts the relay through an ordinary sandbox launcher child and uses successful managed launcher exit as its synchronous cleanup barrier.
  `serve --no-sandbox` starts the relay directly without sandbox policy or runner-owned descendant cleanup.
  Its sandbox access is limited to the launcher's standard streams and ordinary child lifecycle.
  Do not move these responsibilities into the relay.
- The relay owns local worker transports, sideband translation, direct-worker signal delivery, bounded termination, and direct-worker reaping.
  It preserves each producer's order without reconstructing chronology across independent transports.
  It does not own process-tree cleanup or depend on a particular process-group identity or sandbox topology.
- The sandbox frontend selects application policy and execs the verified private runner with launch-time configuration.
  Native enforcement, command status, signals, terminal ownership, descendant retirement, and private storage belong to that executable.
  Do not rebuild native supervision in Console.
  Caller death while the runner lives must still trigger configured cleanup; independent recovery after runner death is unsupported.
  The server retains only ordinary child-process integration and logical worker-generation ownership.
- Restart, replacement, evaluation admission, stdin writes, resolver callbacks, and retained-environment commits are scoped to the worker generation that accepted them.
  Work admitted for an old generation must not reach its replacement.
- R, Python, and DuckDB dependency resolution runs outside the worker sandbox.
  Accept only documented trusted inputs: `ir` package references with `IR_NO_LOCAL_SOURCES`, named PEP 508 registry requirements under the trusted startup resolver configuration, and validated DuckDB extension names.
  Accepted installation or build code may execute with server permissions.
- Treat submitted R, Python, and SQL as shell-class capability and enforce isolation at the worker-process boundary unless `serve --no-sandbox` is selected.
  Keep complete code cells separate from interactive `stdin`, and keep the MCP adapter independent of interpreter implementation details.
- Production R and Python programs under `src/` are included in the binary at compile time.
  The worker and resolvers must not load them from the source tree or installation layout at runtime.

## Repository map

### Public interface and records

- `src/main.rs`, `src/cli.rs` — binary entry point and command definitions.
- `src/server.rs`, `src/server_transport.rs` — MCP tools, stdio transport, and response-delivery ownership.
- `src/transcript.rs`, `src/transcript/{event,markdown,output}.rs` — typed recording events, append-only tool journal, Markdown and source-only Quarto projections, cell output files, and image artifacts.
- `r/` — thin ellmer package that resolves and manages `mcp-console serve` as a persistent tool.

### Protocols, relay, and worker orchestration

- `src/worker_protocol.rs`, `src/sideband.rs` — relay-worker message and framing contract.
- `src/relay_protocol.rs` — server-relay JSONL message and framing contract.
- `src/worker_relay.rs`, `src/worker_relay/event_writer.rs` — worker launch, I/O forwarding, ordered event output, direct-worker signaling, termination, and reaping.
- `src/worker_client.rs`, `src/worker_client/` — session coordination and send planning, server-owned environment, evaluation, lifecycle, ordinary launcher child ownership, ordered event dispatch, output tape, shared Unix relay transport, and platform-specific startup observation.
- `src/process_exit.rs` — ordinary direct-child exit observation without reaping, used by server launcher ownership.
- `src/worker_client/relay_output.rs` — relay output draining bounded by ordinary launcher exit, including a surviving inherited writer.
- `src/sandbox.rs`, `src/sandbox/{installation,runner,unsupported}.rs` — thin sandbox frontend, verified runner selection, application policy, and unsupported-platform errors.
- `src/worker.rs`, `src/worker/core.rs`, `src/worker/embedded_r.rs`, `src/r_repl.c` — worker-facing facade, shared process services, current embedded-R backend, cell dispatch, console callbacks, and the C-owned DLL-REPL boundary.

### Language adapters

- `src/r_bridge.rs` — shared Rust FFI for process-lifetime private R bridge environments.
- `src/python.rs`, `src/python/library.rs`, `src/python/reticulate.rs`, `src/python/initialize.R`, `src/python/bridge.R`, `src/python/runtime.py` — Rust-owned Python runtime facade, CPython initialization, current reticulate backend, R bridges, and Python evaluator runtime.
- `src/sql.rs`, `src/sql/r_dbi.rs`, `src/sql/py_dbapi.rs`, `src/sql/bridge.R`, `src/sql/dbapi.py` — worker-facing SQL router, R DBI and Python DB-API providers, and their runtime bridges.
- `src/r_graphics.rs`, `src/r_graphics.c`, `src/r_graphics/bridge.R` — managed graphics orchestration, C callback boundary, and R bridge.
- `src/r_environment.rs`, `src/r_environment/bridge.R` — live R-library bridge.

### Resolvers and sandbox

- `src/resolver.rs`, `src/resolver/` — retained host environments, direct Python-version selection, validation, platform implementations, and resolver process-group lifecycle.
- `src/resolver/programs/` — compile-time R programs for DuckDB extension preparation, R-library resolution, and `uv` discovery.
- `src/sandbox/runner.rs`, `src/sandbox/policy_extensions.sbpl`, `src/process_descriptors.rs` — immutable runner launch configuration, macOS policy additions, and ordinary child inherited-descriptor boundary.
- `sandbox-runner.json`, `scripts/stage-sandbox-runner`, `build_backend.py`, `build.rs`, `src/sandbox/installation.rs` — pinned source preparation, companion bundle packaging, and streaming artifact verification.

### Tests and development scripts

- `tests/support/` — shared capability requirements, explicit execution fixtures, transcript records, snapshots, normalization, checkpoints, capture, process, platform event, native fixture, macOS, assertion, R, resolver, client, and direct-suite helpers.
- `tests/fixtures/` — deterministic workers, resolvers, package fixtures, searchable native interposers, and boundary-specific relay and worker programs.
- `tests/boundaries/client_server/` — public MCP client-server behavior.
- `tests/boundaries/server_relay/` — private server-relay wire behavior.
- `tests/boundaries/relay_worker/` — worker sideband and standard-stream behavior through the relay.
- `tests/boundaries/cli/` — direct CLI behavior.
- `tests/boundaries/*/sandbox/` — sandbox-specific contracts within their owning boundary; ordinary cases remain under their runtime, protocol, or lifecycle subject.
- `tests/boundaries/*/_harness.py` — boundary-specific process launch and capture mechanics.
- `tests/boundaries/_run.py`, `tests/transcript_runner.py` — recursive transcript discovery, selection, location, snapshot checking, progress reporting, and runner regressions.
- `tests/architecture.py` — sandbox dependency-direction checks and their command-line regressions.
- `tests/snapshots/` — generated YAML 1.2 snapshots, parallel to the boundary test hierarchy.
- `r/tests/testthat/` — R package protocol and ellmer adapter tests.
- `scripts/release.py`, `tests/release.py` — release validation and installed-wheel acceptance.
- `tests/install.py`, `tests/sandbox_installation.py` — unstaged uv installation, relocated bundle acceptance, and private companion verification.
- `scripts/test` — release binary build and selected transcript execution.
- `scripts/validate_runtime_sources.py` — extracted R/Python inventory and syntax validation.
- `scripts/format`, `scripts/check-core`, `scripts/check` — formatting, core checks, and repository-wide checks.

## Working rules

- Before merging any PR, require passing CI and a verified thumbs-up reaction from the GPT connector reviewer for the current PR head.
  Check the actual GitHub reaction; a completed review or absence of findings is not approval.
- Follow `RELEASE.md` before pushing a release tag.
  Never yank or remove published PyPI files.
- Keep PRs coherent and easy to review.
  For behavior-changing implementation, aim for fewer than 200 added and deleted lines as a heuristic.
  Mechanical moves, internal-only reorganization, tests, snapshots, and documentation do not count toward it.
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
