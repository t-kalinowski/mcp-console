# AGENTS.md

This file contains repository-wide instructions and a navigation map.
Keep it synchronized with implemented code.
Start with the [development routes and validation ladder](docs/DEVELOPMENT.md) to find a focused public test and the next validation command.
Detailed current behavior belongs in the documents indexed by `docs/README.md`, source, and public transcript tests.

The documents under `design-sketches/` describe intended behavior, not the current implementation.
They are exploratory sketches, not production specifications, and need not be complete or mutually consistent.
Do not apply the same correctness and consistency requirements to sketches as to production code and documentation of implemented behavior.
Reconcile the relevant contracts, tests, and current documentation when implementing a proposal.

## Sources of truth

- `README.md` describes the current user-facing project status.
- `RELEASE.md` defines release preparation, wheel rehearsal, publication, verification, and recovery.
- `docs/README.md` maps the implemented documentation by audience.
- `docs/ARCHITECTURE.md` describes the implemented process structure, ownership, and lifecycle.
- `docs/CONFIGURATION.md` defines project-file discovery, ordered CLI overrides, inline syntax, and schema-independent merge rules.
- `docs/SANDBOX.md` describes application policy, runner integration, supported hosts, and lifetime guarantees.
- `docs/SANDBOX_CONFIGURATION.md` defines the public configuration interface, environment ownership, caller examples, and transport integrity.
- `docs/DOCKER.md` defines container targets, image setup, preinstalled environments, owned retirement, and controller records.
- `docs/DOCKER_SANDBOX.md` defines the standalone SBX provider, prepared templates, shared paths, inherited policy, owned microVM retirement, and controller records.
- `docs/SSH.md` defines remote target configuration, runtime prerequisites, managed preparation, launch framing, retirement confirmation, and local recording semantics.
- `docs/LINUX_COMPATIBILITY.md` records capability requirements, security comparisons, native backend differences, and tested Linux baselines.
- `docs/SANDBOX_RUNNER_INTEGRATION.md` records the migration baseline, fixture changes, supported-host validation, and changed guarantees.
- `docs/PYTHON.md` describes the synchronous and asynchronous Python clients and framework integrations.
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

Project configuration is read only from `.agents/console/config.yaml` in the launch working directory.
Repeated `-c KEY=VALUE` overrides apply in command-line order before application decoding and validation.
Keep `src/config.rs` and its parsers independent of application field names: mappings merge recursively, while lists, scalars, and explicit null replace prior values.
Top-level `extends` selects the native `":workspace"` or `":read-only"` built-in; omission preserves the default policy.
Capture the workspace once at trusted launch and retain it across worker generations.
Reuse native constructors and path handling, and keep explicit native adjustments subject to native precedence.
Console adds `.claude` as a read entry and excludes shared temporary write grants by default for `":workspace"`; metadata defaults are deliberately overridable.
Recorded sessions, transcripts, outputs, and artifacts are written beneath `.agents/console/sessions/`.

An optional `target` independently selects transport and compute for `serve`, including `--no-sandbox`: SSH host, local Docker container, or local Docker Sandbox microVM.
Omitted target and explicit local host selection share the existing local launch path.
Capture its required absolute remote workspace, executable prefix, and raw user policy locally once; materialize paths, platform additions, and native preflight on that execution host without rediscovering YAML.
SSH discovers capability and executes managed preparation on the remote host, independently of the relay and worker.
Its default command uses remote PATH `mcp-console`, falling back to `uvx mcp-console` only when absent; configured argv prefixes run without executable preflight validation.
The preparation owner captures trusted resolver settings once; the local server owns requirements, candidates, and activation decisions.
Only explicit remote R_HOME and RETICULATE_PYTHON workload selections also inform preparation.
Never discover controller interpreters, invoke controller resolvers, or validate remote paths on the controller.
Require explicit result and resolver cleanup confirmation before committing an environment; uncertain preparation retirement blocks further preparation and replacement.
Docker resolves its image once before workload startup and uses that immutable ID for every probe and generation.
Each generation owns a fresh Linux container containing relay and worker; a local owner observes server and attachment loss and requires confirmed container removal before replacement.
Docker uses image packages with dynamic preparation disabled, even if resolvers are installed.
Docker Sandbox selects compute enforcement by default; explicit `sandbox.provider: compute` documents that selection.
All other targets default to native enforcement.
Keep this selector separate from native policy JSON and from whether direct launch needs an inner native runner.
The SBX adapter accepts only workload environment controls, rejects native restrictions/extends/writable roots, and never discovers or executes the native companion.
Use fixed standalone sbx CLI argument arrays and structured output, never a private daemon API or host Docker substitute.
Capture a prepared digest-qualified template once, use one newly owned microVM per probe or generation, and require forced removal plus authoritative absence before replacement.
An unacknowledged create remains uncertain after an empty listing; never adopt or prefix-match user resources.
Docker owns inherited policy and host integrations; Console must not mutate global policy, credentials, or daemon settings.
VM-local changes disappear on restart; declared host shares and any records beneath them remain exposed according to provider access.
Standalone `sandbox` remains local for supported native selections and rejects resolved compute enforcement.
SSH exit alone cannot confirm remote retirement; require the remote launcher's terminal acknowledgment before replacement, and block replacement after unconfirmed cleanup.

The worker relay, built-in worker, and managed resolvers support macOS and Linux.
The default sandbox and standalone sandbox command support both platforms.
Default Linux sandbox execution requires procfs, permitted native namespace operations, and the selected policy enforcement capabilities; see `docs/LINUX_COMPATIBILITY.md` for tested baselines, explicit enforcement modes, and constrained-host behavior.
Do not infer support from a kernel version alone.
Windows is not supported.
Other Unix operating systems are not supported build or runtime targets; shared `cfg(unix)` modules do not imply support for them.
Retain platform conditionals for modules that use OS-specific APIs and for selecting different implementations or unsupported-platform stubs; avoid redundant gates on shared code.
CI runs core checks and all capability-applicable transcript modes on macOS and Linux.
External SSH tests automatically use a reachable optional host, with selection and availability confined to `tests/support/ssh_external.py`; absent hosts skip those cases while localhost SSH coverage remains available.
Keep Python SDK integration test dependencies free of exact version pins, retain the published dependency lower bounds, and constrain MCP to the supported major using `==2.*`.
Keep one CI job per platform.
CI restores Cargo build data across source and dependency changes within the same OS version, architecture, toolchain, applicable R version, and UTC week, with incremental compilation enabled.
Use `ImageOS` in cache keys; log the full `ImageVersion` without including it in cache identities.
Keep every GitHub Actions cache key and restore prefix within the current UTC ISO week, including uv, IR/renv, R package libraries, downloads, source archives, and finished build outputs.
The first CI run with a fresh weekly uv cache resolves current SDK releases; later runs may reuse that environment.
Skip runner staging only when both its finished artifacts and build data are exact cache hits.
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
Staging, packaging, and validation share checkout ownership outside `target`; conflicts fail with the lock path and last recorded owner details.
Wrap direct Cargo and Maturin commands in `scripts/with-checkout` to claim that ownership.
See `RELEASE.md` for prerequisites, bundle layout, build caches, and the explicit source-checkout override.
Run commands from the repository root:

```text
scripts/format
scripts/check
scripts/test [BOUNDARY/SUITE[::CASE]]
scripts/test --list
scripts/test --update BOUNDARY/SUITE[::CASE]
```

`scripts/format` attempts Ruff, Yamark, rustfmt, Air, and the embedded fixture checker in sequence and reports each result.
A missing or failing step does not prevent the remaining steps from running; the default exits successfully, while `--strict` returns failure if any step failed.
Review its output and resulting changes.
`scripts/check-fixtures` checks marked static R/Python programs, directives, and `code()` indentation; see `tests/boundaries/AUTHORING.md` for dynamic templates and intentional invalid syntax.
Validation records and phase logs remain in `.dev-workflow/runs/`; see `docs/DEVELOPMENT.md` for ownership and the host concurrency budget.
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
  `serve --no-sandbox` skips the native runner at the selected target.
  Docker containers and Docker Sandbox microVMs retain their outer enforcement and retirement; host execution retains direct-worker cleanup limits.
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
- `src/config.rs`, `src/config/{inline,yaml}.rs` — schema-independent project-file loading, inline override parsing, and recursive configuration layering.
- `src/settings.rs`, `src/settings/target.rs` — project-path selection and application settings decoded after layering and retained across worker launches; native policy values remain JSON; the sandbox layer adds application launch requirements and delegates validation and defaults to the runner.
- `src/ssh.rs` — configured OpenSSH transport and remote retirement confirmation.
- `src/target_launch.rs`, `src/target_launch/` — shared versioned bootstrap, relay envelope, direct/native launcher mechanics, image runtime selection, workload environment decoding, cancellable CLI transfer, and the shared local owner request and observation.
- `src/target_session.rs` — selected SSH/Docker/SBX sessions, shared compute probes and controller replacement blocking, and generation-owned retirement receipts and resource names.
- `src/docker.rs`, `src/docker/` — captured Docker endpoint and immutable image setup, local ownership helper, and confirmed container retirement.
- `src/docker_sandbox.rs`, `src/docker_sandbox/owner.rs` — compute policy validation, typed SBX CLI adapter, prepared template identity, owned microVM creation, and confirmed retirement.
- `src/ssh/preparation.rs`, `src/ssh/preparation/{client,host}.rs` — typed trusted preparation connection, remote startup configuration, operation-scoped resolver control, and confirmed results.
- `src/resolver/execution.rs` — host selection for existing resolver operations, preserving local session transactions.
- `src/server.rs`, `src/server/execution.rs`, `src/server_transport.rs` — MCP tools, descriptions derived from effective target/provider metadata, stdio transport, and response-delivery ownership.
- `src/transcript.rs`, `src/transcript/{event,markdown,output}.rs` — typed recording events, append-only tool journal, Markdown and source-only Quarto projections, cell output files, and image artifacts.
- `python/mcp_console/` — synchronous and asynchronous MCP clients and composable framework adapters.
  The public `openai.py`, `anthropic.py`, `chatlas.py`, and `codex.py` modules group adapters by product or SDK.
  SDK registration uses these adapters with the live MCP schema; `send()` and the callable console object are ordinary Python interfaces, not SDK schema providers.
- `r/` — thin ellmer package that resolves and manages `mcp-console serve` as a persistent tool.

### Protocols, relay, and worker orchestration

- `src/worker_protocol.rs`, `src/sideband.rs` — relay-worker message and framing contract.
- `src/readiness.rs` — shared blocking descriptor readiness and cancellation waits.
- `src/input_watch.rs`, `src/input_watch/` — platform input-closure observation shared by startup and the compute ownership helpers.
- `src/relay_protocol.rs` — server-relay JSONL message and framing contract.
- `src/worker_relay.rs`, `src/worker_relay/event_writer.rs` — worker launch, I/O forwarding, ordered event output, direct-worker signaling, termination, and reaping.
- `src/worker_client/output.rs`, `src/worker_client/output/{tape,preview,terminal}.rs` — canonical response composition, streaming output cuts, bounded 8 KiB text previews, independent image admission, raw-file receipts, and progress projection.
- `src/worker_client.rs`, `src/worker_client/` — session coordination and send planning, server-owned environment, evaluation, lifecycle, ordinary launcher child ownership, ordered event dispatch, output tape, shared Unix relay transport, and platform-specific startup observation.
- `src/process_exit.rs` — ordinary direct-child exit observation without reaping, used by server launcher ownership.
- `src/process_output.rs` — output draining bounded by an owned child exit, including a surviving inherited writer; used for local launchers, the SSH child, and the remote helper's launcher without equating their cleanup guarantees.
- `src/sandbox.rs`, `src/sandbox/{installation,runner,unsupported}.rs` — thin sandbox frontend, verified runner selection, application policy, and unsupported-platform errors.
- `src/worker.rs`, `src/worker/{coordinator,core,input}.rs` — worker facade, language coordination, shared sideband services, and interactive stdin buffering.
- `src/worker/embedded_r.rs`, `src/r_repl.c` — R runtime, native events, graphics, console callbacks, and the C-owned DLL-REPL boundary.

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
- `tests/support/ssh_external.py`, `tests/fixtures/ssh_install.py` — optional real-host availability, test-owned source installation and build cache, and temporary remote workspace setup.
- `tests/fixtures/` — deterministic workers, resolvers, package fixtures, searchable native interposers, and boundary-specific relay and worker programs.
- `tests/boundaries/client_server/` — public MCP client-server behavior, including real Python SDK integrations under `integrations/`.
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
- `checkout_workflow.py`, `scripts/with-checkout`, `tests/workflow.py` — shared checkout ownership, validation records, host concurrency, and public command regressions.

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
  Choose its owning modules, public cases, expected snapshots, and intended base before cross-cutting implementation; use the planning template and `scripts/review-diff` in `docs/DEVELOPMENT.md`.
- For a public behavior change, first add a public acceptance or regression test and confirm it fails.
  Verify an internal-only refactor with the existing public suite.
  Test public interfaces, not private helpers.
- For internal coordination and lifecycle control, prefer blocking event-driven waits with an explicit wakeup path.
  Do not use busy loops or short fixed-interval polling when the state transition can notify a condition variable, descriptor, or platform event.
- Preserve client-visible runtime output in transcript snapshots, including complete errors and tracebacks.
  Normalize only incidental values such as run-specific temporary paths; do not replace behavior with summaries or placeholders.
  Synthetic stress repetitions may use lossless text-and-count notation after exact full-response assertions; see `tests/support/evidence.py`.
- Keep embedded R, Python, SQL, and shell fixture programs as readable multiline strings.
  Use escapes such as `\n` only when the character is data.
- Put `# fmt: r` or `# fmt: python` immediately before each embedded R or Python test program, including `code(...)` calls nested inside other calls.
  Indent the payload and closing delimiter one Python indentation level deeper than the line containing `code(`, preserving the embedded program's own indentation.
  Recheck this indentation after running `scripts/format` and in the committed source.
  When formatting a shared payload, refresh each platform's affected snapshots, including cases skipped on the current host.
- Keep related arguments grouped so formatters preserve their relationship.
  Use separate `.args([option, value])` calls for command options and `c(label, value, "\n")` groups for related R `cat()` diagnostics, preserving the command arguments and printed output.
- Refactor internal modules when the implemented responsibilities have a clearer boundary.
  Do not add structure for planned behavior.
  Treat roughly 500 lines of production source as a prompt to reassess a file, not a hard limit, and keep one Cargo package until the implementation presents a concrete crate boundary.
- Update design documents in the same PR only when they describe changed behavior.
  Update this file when repository-wide constraints or navigation change.
- Run `scripts/format` unchanged and review its changes before every commit.
  Run `scripts/check` before opening a PR.
