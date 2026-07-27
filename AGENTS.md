# AGENTS.md

This file is the durable project context for coding agents working on MCP Console.
Keep it current as the repository changes.
It should remain sufficient to understand the product direction, locate the relevant implementation, and avoid reopening settled architectural decisions.

## Product intent

MCP Console is a persistent, sandboxed data-science workbench exposed through MCP.
A named session contains one R process, one Python interpreter embedded through reticulate, and one persistent DuckDB connection.
The agent can alternate among R, Python, and SQL while retaining objects, imports, package state, database state, debugger state, and files.

The public abstraction is a **console session**.
Normal top-level input is a complete R, Python, or SQL cell.
Line-oriented `stdin` is used only when the active evaluation explicitly requests input, including R `readline()` or `browser()` and Python `input()` or a debugger.

The tool must be cheap enough to remain globally enabled.
The common calls should look like:

```text
{"python":"..."}
{"r":"..."}
{"sql":"..."}
{}
```

The empty object waits for or polls the default session.
Large results must not flood model context.
Replies are bounded text; complete explicit stream output and generated artifacts live in ordinary session files.
Each session maintains a generated, non-executing `transcript.qmd` that an agent can read after context compaction and use as source material for a refined notebook, script, or report.

MCP Console is effectively shell-class capability.
Safety is enforced around the worker process and its descendants, not by filtering language source.

## Settled product decisions

- Product and binary name: `mcp-console`.
- Frequent MCP tool: `console`.
- Low-frequency environment and lifecycle tool: `console_session`.
- Language is selected by the present object key: `r`, `python`, or `sql`.
- Top-level submissions are complete cells, not line-by-line parser input.
- `stdin` is accepted only for an already-active input consumer.
  It is exact stream text, may contain multiple lines, and receives no implicit newline.
- A named session runs at most one top-level evaluation at a time.
- Sessions are created lazily by the first code submission.
- Independent or parallel work uses separately named sessions.
- MCP results are text-only; v1 has no `outputSchema`, structured-content mirror, MCP resource dependency, or inline image result.
- Oversized explicit output is retained in session files; each response contains only a bounded current excerpt.
- Large values and SQL relations are previewed structurally before full textual materialization.
- The agent-facing durable record is `transcript.qmd`; a granular JSONL journal is internal implementation state.
- Requirements are additive logical-session configuration managed by `console_session`.
  They survive runtime restarts and are not accepted on ordinary `console` calls.
- Interrupt, restart, close, and worker crash are distinct observable events.
  `restart` loses in-memory R, Python, SQL, debugger, and process state while retaining requirements, workspace files, and transcript.
  Never silently replace a crashed worker.

See [`VISION.md`](VISION.md) for the fuller rationale and [`docs/MCP_INTERFACE.md`](docs/MCP_INTERFACE.md) for normative public behavior.

## Settled runtime decisions

- One logical session maps to one sandboxed worker process and one runtime generation.
- R is the host runtime and owns the process main thread.
- Python is embedded into that R process through reticulate.
- SQL initially uses one persistent DuckDB connection through the DuckDB R package and DBI.
- The DuckDB CLI is not run or embedded.
  Its bounded table presentation and interactive ergonomics are references only.
- The supervisor and worker use a small private protocol specialized for evaluation, output, interactive input, and control.
- Do not use the Jupyter wire protocol internally.
- Do not run Ark as the session worker.
- Reuse `harp`, `libr`, and existing `mcp-repl` frontend code where practical.
  Isolate their unstable or internal APIs behind a narrow adapter and pin compatible revisions.
- The long-term preferred upstream shape is a reusable native R frontend/runtime layer shared by Ark, `mcp-repl`, and MCP Console.
  Do not fork difficult frontend logic casually.

### Evaluation boundaries

The worker receives structured commands such as `EvaluateCell`, `ProvideInput`, and `Interrupt`.
Complete code cells and interactive input must never share one undifferentiated line queue.

R cells are parsed and evaluated directly at a native top-level boundary on the R thread.
Preserve console visible-value behavior without calling a user-visible R wrapper such as `eval(str2expression(...))`, `source()`, or `withAutoprint()` around the entire cell.
A user call to `sys.calls()` should not contain an MCP Console dispatcher frame merely because the code came from the tool.

`ReadConsole` remains installed for genuine nested console input during an active evaluation.
It must not be the transport for ordinary top-level cells.

Python uses a persistent cell executor in `__main__`, with a synthetic source filename, statement support, final-expression display, and language-native tracebacks.
Do not use `reticulate::repl_python(input = ...)` as the core cell evaluator.
A minimal R/reticulate bridge frame is acceptable in the initial implementation and must be treated as an explicit boundary.

SQL initially crosses a minimal private R bridge into DBI/DuckDB.
During an SQL evaluation, R introspection reached through a callback may therefore see the console SQL bridge and DBI frames.
Do not fabricate or rewrite `sys.calls()` to hide real boundaries.
Store source out of band and pass a short evaluation ID through bridge calls so large cells do not appear in R call expressions.

## Repository sitemap

The exact tree may evolve.
Update this map whenever ownership moves.

### Root documents

- `README.md` — user-facing overview, status, examples, and document index.
- `VISION.md` — product purpose, goals, non-goals, and success criteria.
- `AGENTS.md` — durable agent context, settled decisions, repository map, and working rules.
- `docs/MCP_INTERFACE.md` — normative agent-facing schema and observable behavior.
- `docs/TOOL_DESCRIPTIONS.md` — exact registered tool and property descriptions.
- `docs/ARCHITECTURE.md` — implementation architecture and staged plan.

Create focused documents only when a subsystem has enough detail to justify a separate source of truth:

- `docs/WORKER_PROTOCOL.md` — exact private IPC messages and ordering.
- `docs/OUTPUT_AND_TRANSCRIPTS.md` — output timeline, spools, previews, QMD generation, and retention.
- `docs/SANDBOX.md` — platform policies and inherited-host sandbox behavior.
- `docs/DEPENDENCIES.md` — R/Python requirement grammar, resolution, caching, and activation.
- `docs/TESTING.md` — supported runtime matrix, integration harness, fixtures, and snapshot rules.
- `docs/adr/` — small decisions whose alternatives and consequences need a durable record.

### Planned source layout

- `src/main.rs` — dispatch MCP supervisor, worker, installation, and diagnostics modes.
- `src/cli.rs` — command-line options.
- `src/config.rs` — validated server, output, runtime, retention, and sandbox configuration.

- `src/mcp/` — thin MCP transport and tool adapter.
  - Defines `console` and `console_session` schemas.
  - Validates public arguments.
  - Converts service results to bounded MCP text.
  - Contains no interpreter mechanics.

- `src/session/` — product-level named-session state machine.
  - Owns generations, lazy creation, state transitions, waiters, and lifecycle operations.
  - Rejects new code while a session is running or waiting for input.

- `src/worker/` — worker process launch, supervision, private protocol, and control path.
  - The normal command path may block behind evaluation.
  - Interrupt and forced termination must have an independent high-priority path.

- `src/runtime/r/` — R discovery, startup, frontend callbacks, native cell evaluation, conditions, visible values, graphics, and interrupt recovery.
  - `ReadConsole` handles interactive input only.
  - Keep all direct R API calls on the owning thread.
  - Hide `harp`/`libr` specifics behind this module.

- `src/runtime/python/` — reticulate initialization, Python cell executor, exceptions, display hook, stdin/debugger bridge, and R/Python object access.

- `src/runtime/sql/` — persistent DuckDB/DBI connection, statement execution, bounded fetching, table previews, environment scanning, and explicit relation registration.

- `src/runtime/` shared modules — submission IDs, language-neutral input requests, display values, and runtime outcomes.

- `src/environment/` — additive R/Python manifests, resolver process, immutable caches, activation, and provenance.

- `src/output/` — managed and raw stream intake, per-evaluation spools, reply cursors, value/table previews, and final response budgets.

- `src/transcript/` — internal journal model and generated Quarto projection.
  The QMD must be rebuildable and must not be edited in place.

- `src/sandbox/` — worker-process filesystem, network, subprocess, resource, and host-policy enforcement.

- `tests/` — real-binary MCP integration tests grouped by public behavior.
- `fixtures/` — deterministic data, fake workers, and normalized expected output.
- `scripts/` — local equivalents of CI integration and session-inspection workflows.

## Working rules

1. Preserve the small public surface.
   Prefer ordinary language code, runtime helpers, files, or private protocol extensions over new MCP tools or frequently visible fields.
2. Treat `docs/MCP_INTERFACE.md` as the observable contract.
   Public behavior changes require corresponding integration tests and documentation in the same patch.
3. Keep complete cells separate from `stdin`.
   Never route top-level code through the runtime input queue.
4. Never infer idle, completion, debugger state, or input state from visible prompt strings.
5. Keep arbitrary R, Python, SQL, native-library, and child-process execution inside the sandboxed worker.
6. Keep the MCP supervisor responsive and free of user-loaded native libraries.
7. Bound output before it becomes MCP text.
   Do not stringify or collect an arbitrary object or relation in full and truncate afterward.
8. Polls return only newly observed bounded output and current state.
   Unseen overflow stays in the evaluation spool and is not forced through later calls.
9. Keep the internal journal and generated QMD separate.
   The journal may be granular; the QMD is the readable agent artifact.
10. Do not silently restart a crashed worker.
    Preserve honest state-loss and generation boundaries.
11. Test through the built binary and real runtimes.
    Use unit tests for local algorithms, not as a replacement for MCP integration tests.
12. Add acceptance tests for R stack fidelity: ordinary top-level R evaluation must not introduce a console-owned R closure frame.
13. Isolate backend-specific and unstable API use behind one adapter with a pinned compatibility test.
14. Record substantial unresolved choices as focused spikes or ADRs, then update this file when they become settled.
15. Keep this sitemap accurate.
    An agent should be able to find the owner of a behavior without broad exploratory search.

## First implementation priorities

1. MCP server skeleton, tool validation, session state machine, and deterministic fake worker.
2. One persistent R worker using `harp`/`libr`, direct top-level cell evaluation, structured input events, interrupt, restart, and crash reporting.
3. Bounded output spools, reply cursors, value previews, and generated `transcript.qmd`.
4. Reticulate Python cell execution and interactive input.
5. Persistent DuckDB through R/DBI, bounded SQL results, R environment scanning, and explicit registration.
6. Atomic dependency preparation.
7. Cross-platform sandbox and resource hardening.
