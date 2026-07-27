# AGENTS.md

Keep this file current so a new agent can locate ownership and avoid reopening settled decisions.

## Product contract

MCP Console is a persistent, sandboxed data-science workbench exposed through MCP.
A named console session owns one worker process containing R, reticulate Python, and a persistent DuckDB connection.
Across ordinary calls, the session retains objects, imports, loaded packages, database state, debugger state, and files.

The binary is `mcp-console`.
The public surface must remain cheap enough to keep globally enabled:

```text
{"python":"..."}
{"r":"..."}
{"sql":"..."}
{}
```

- `console` is the frequent tool.
  The present `r`, `python`, or `sql` key selects the language and contains one complete cell.
  An empty call waits for or polls the default session.
- `stdin` is accepted only for an active input consumer such as R `readline()` or `browser()`, Python `input()`, or a debugger.
  It is exact stream text, may contain multiple lines, and receives no implicit newline.
- `console_session` manages infrequent environment and lifecycle operations.
  Requirements configure the logical session, are additive, survive restart, and are not accepted by `console`.
- Sessions are created by the first code submission and run at most one top-level evaluation at a time.
  Parallel work uses separately named sessions.
- Replies are bounded text.
  Large values and SQL relations are previewed structurally before full materialization; complete explicit streams and generated artifacts remain in session files.
  v1 has no `outputSchema`, structured-content mirror, MCP resource dependency, or inline image result.
- Each session generates a non-executing `transcript.qmd` so an agent can recover work after context compaction and derive refined artifacts.
  The granular JSONL journal is internal state.
- Interrupt, restart, close, and worker crash are distinct events.
  Restart loses in-memory R, Python, SQL, debugger, and process state while retaining requirements, workspace files, and transcript.
  A crashed worker remains stopped until explicitly restarted or closed.

## Runtime constraints

MCP Console exposes shell-class capability.
Enforce safety around the worker and its descendants, not by filtering language source.

- One logical session maps to one sandboxed worker process and one runtime generation.
- R is the host runtime and owns the process main thread.
  Python is embedded through reticulate.
  SQL initially uses one persistent DuckDB connection through the DuckDB R package and DBI.
- The supervisor and worker use a small private protocol for evaluation, output, interactive input, and control.
  Do not use the Jupyter wire protocol or run Ark as the worker.
  Do not run or embed the DuckDB CLI; use it only as a presentation and ergonomics reference.
- Reuse `harp`, `libr`, and existing `mcp-repl` frontend code where practical.
  Keep their unstable or internal APIs behind one narrow adapter; pin revisions and add a compatibility test.
  Prefer an upstream native R runtime layer shared by Ark, `mcp-repl`, and MCP Console over a local fork.

### Evaluation boundaries

- Complete cells, interactive input, and interrupts use distinct commands such as `EvaluateCell`, `ProvideInput`, and `Interrupt`.
  Never route cells through an undifferentiated line queue.
- Parse and evaluate R cells directly on the R thread at a native top-level boundary.
  Preserve visible-value behavior without wrapping the cell in `eval(str2expression(...))`, `source()`, or `withAutoprint()`.
  A user call to `sys.calls()` must not gain a console-owned R closure frame.
  `ReadConsole` handles only genuine nested input.
- Python uses a persistent cell executor in `__main__` with a synthetic source filename, statement support, final-expression display, and language-native tracebacks.
  Do not use `reticulate::repl_python(input = ...)` as the core evaluator.
  Treat the minimal R/reticulate bridge frame as an explicit boundary.
- SQL crosses a minimal private R bridge into DBI/DuckDB.
  R introspection through a callback may see real SQL bridge and DBI frames; do not fabricate or rewrite `sys.calls()` to hide them.
  Store source out of band and pass a short evaluation ID so large cells do not appear in R call expressions.

## Sources of truth

- `README.md` — user-facing overview, current status, examples, and document index.
- `VISION.md` — rationale, goals, non-goals, and success criteria.
- `docs/MCP_INTERFACE.md` — normative public schema and behavior.
- `docs/TOOL_DESCRIPTIONS.md` — exact registered tool and property descriptions.
- `docs/ARCHITECTURE.md` — implementation design, testing strategy, and current implementation sequence.

Create a focused source of truth only when a subsystem needs more detail:

- `docs/WORKER_PROTOCOL.md` — private IPC messages and ordering.
- `docs/OUTPUT_AND_TRANSCRIPTS.md` — output, previews, spools, QMD generation, and retention.
- `docs/SANDBOX.md` — platform policy and inherited-host behavior.
- `docs/DEPENDENCIES.md` — requirement grammar, resolution, caching, and activation.
- `docs/TESTING.md` — runtime matrix, integration harness, fixtures, and snapshots.
- `docs/adr/` — decisions whose alternatives and consequences need a durable record.

## Planned source ownership

Update this map when ownership changes.

- `src/main.rs`, `src/cli.rs`, `src/config.rs` — process modes, CLI, and validated configuration.
- `src/mcp/` — tool schemas, public validation, and bounded MCP responses; no interpreter mechanics.
- `src/session/` — names, generations, lazy creation, state transitions, waiters, and lifecycle.
  It rejects new code while a session is running or waiting for input.
- `src/worker/` — worker launch, supervision, private protocol, and control.
  Evaluation may block normal commands; interrupt and termination use an independent high-priority path.
- `src/runtime/` — shared submission IDs, input requests, display values, and outcomes.
  - `r/` — R discovery, startup, callbacks, evaluation, conditions, visible values, graphics, and interrupt recovery.
    All direct R API calls stay on the owning thread; `harp` and `libr` details stay here.
  - `python/` — reticulate initialization, cell execution, exceptions, display, input/debugger bridge, and R/Python access.
  - `sql/` — persistent DuckDB/DBI connection, execution, bounded fetching, previews, R-environment scanning, and explicit registration.
- `src/environment/` — additive manifests, resolution, immutable caches, activation, and provenance.
- `src/output/` — managed and raw streams, per-evaluation spools, reply cursors, previews, and response budgets.
- `src/transcript/` — internal journal and generated Quarto projection.
  The QMD must be rebuildable and must not be edited in place.
- `src/sandbox/` — worker filesystem, network, subprocess, resource, and host-policy enforcement.
- `tests/` — real-binary MCP integration tests organized by public behavior.
- `fixtures/` — deterministic data, fake workers, and normalized expected output.
- `scripts/` — local CI and session-inspection workflows.

## Change rules

1. Treat `docs/MCP_INTERFACE.md` as the observable contract.
   Public behavior changes require integration tests and documentation in the same patch.
2. Prefer language code, runtime helpers, files, or private protocol extensions over new MCP tools or frequently visible fields.
3. Derive runtime state from structured events, never visible prompts.
4. Keep arbitrary R, Python, SQL, native-library, and child-process execution in the worker.
   Keep the supervisor responsive and free of user-loaded native libraries.
5. Bound values and streams before they become MCP text.
   Never collect or stringify an arbitrary object or relation in full and truncate afterward.
6. Polls return only newly observed bounded output and current state.
   Unseen overflow remains in the evaluation spool and is not forced through later calls.
7. Test public behavior through the built binary and real runtimes.
   Use unit tests for local algorithms, not as a substitute for MCP integration tests.
8. Include acceptance coverage that ordinary top-level R evaluation adds no console-owned R closure frame.
9. Record substantial unresolved choices as focused spikes or ADRs, then update this file when they become settled.
