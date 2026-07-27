# MCP Console Architecture

**Status:** Draft implementation scaffold v0.2  
**Date:** 2026-07-26  
**Companion documents:** [`../VISION.md`](../VISION.md), [`MCP_INTERFACE.md`](MCP_INTERFACE.md)

## 1. Purpose

This document describes the implementation boundaries that support the public MCP Console behavior. It is intentionally more specific than a vision document but less rigid than a private wire-protocol specification.

The architecture must support:

- a compact, high-frequency MCP interface;
- persistent R, Python, and SQL state in one session process;
- complete-cell evaluation plus genuine interactive stdin;
- precise state, interrupt, and failure behavior;
- bounded model-facing output with complete retained streams;
- a generated Quarto transcript;
- process-level isolation around arbitrary code.

## 2. Architectural summary

MCP Console is one Rust binary with two process modes:

```text
mcp-console                 MCP supervisor/server
mcp-console --worker ...    one sandboxed session worker
```

The supervisor owns MCP, named sessions, package preparation, worker lifecycle, output budgets, session files, and transcript projection. It never loads R, Python, DuckDB, or arbitrary user native libraries.

Each named session owns one worker process. The worker embeds R on its main thread. Reticulate embeds one Python interpreter inside that R process. The DuckDB R package and DBI initially own one persistent in-memory SQL connection.

```text
MCP client
    │ MCP stdio
    ▼
Rust supervisor
    ├── MCP adapter
    ├── session manager
    ├── dependency resolver
    ├── worker supervisors
    ├── output/transcript pipeline
    └── sandbox policy
          │
          ├── worker: "default"
          │     └── embedded R
          │           ├── persistent R user environment
          │           ├── reticulate Python __main__
          │           └── persistent DuckDB connection
          │
          └── worker: "model-fit"
                └── embedded R + Python + DuckDB
```

The supervisor and worker use a small private protocol specialized for evaluation, output, interactive input, and control. They do not communicate through Jupyter.

## 3. Core invariants

1. Arbitrary user code runs only in a sandboxed worker.
2. One logical session maps to one worker process and one active generation.
3. R, Python, and SQL for a session inhabit the same worker process.
4. The worker main thread owns all direct calls into R.
5. A worker executes at most one top-level evaluation at a time.
6. Complete cells and interactive stdin are different internal command types.
7. Complete cells are never transported through `ReadConsole`.
8. Native R cells do not acquire a console-owned interpreted R frame.
9. Runtime state comes from structured events, never prompt-string matching.
10. Interrupt and termination control cannot wait behind the evaluation command queue.
11. Every MCP response is bounded.
12. Full explicit stream output remains outside model context.
13. Known large values are previewed before full textual materialization.
14. Reset and crash create explicit state-loss boundaries.
15. The QMD transcript is generated from a more authoritative internal record.
16. Ark and Jupyter are references or optional future adapters, not worker runtime dependencies.
17. The public MCP schema grows only when language code, runtime helpers, or files cannot express the workflow.

## 4. Runtime foundation: custom worker, not Ark

### 4.1 Decision

Do not run the full Ark kernel as the MCP Console worker. Build a purpose-specific native R worker using `libr` and `harp`, preferably by refactoring and extending the existing `posit-dev/mcp-repl` runtime or extracting reusable lower-level runtime machinery from Ark.

Ark remains an important implementation reference and possible source of reusable code.

### 4.2 What Ark would provide

Ark already solves difficult native-R frontend concerns:

- R discovery and startup;
- ownership of R's main thread;
- native frontend callbacks;
- structured cell execution;
- stdout, stderr, conditions, errors, and plots;
- interactive input;
- interruption and shutdown;
- source references;
- integrated debugger machinery;
- platform-specific R behavior.

These are substantial and should not be casually reimplemented without comparing Ark's behavior and tests.

### 4.3 Why the full Ark process is not the chosen boundary

Ark's public runtime boundary is an R Jupyter kernel. MCP Console would still need to add or translate:

- first-class R, Python, and SQL cell types;
- compact MCP-specific wait and polling semantics;
- text-only bounded output and sidecar files;
- named sessions and dependency manifests;
- generated QMD transcripts;
- sandbox propagation;
- MCP lifecycle operations.

Running Ark would also bring Jupyter connection management and IDE-oriented LSP, DAP, comm, and frontend machinery that the initial product does not consume directly. Python and SQL cells would still appear to Ark as hidden R wrapper evaluations unless Ark itself were extended.

The chosen boundary therefore favors a smaller runtime whose central request type is already multi-language:

```rust
enum Language {
    R,
    Python,
    Sql,
}

struct EvaluateCell {
    id: EvaluationId,
    language: Language,
    source: String,
    label: Option<String>,
}
```

### 4.4 Dependency strategy for `harp` and `libr`

`harp` and `libr` are low-level building blocks, not a stable complete console API. Isolate them behind `runtime/r/ffi` and pin the exact compatible revision. Do not let their types spread through session, MCP, output, or transcript modules.

Before implementation expands, choose one of these paths explicitly:

1. depend on pinned Ark crates;
2. extract a supported shared native-R runtime crate;
3. vendor a narrowly scoped compatible subset;
4. continue from `mcp-repl` while regularly comparing behavior with Ark.

Record the choice and upgrade policy in an ADR once the first R worker spike is complete.

## 5. Repository layout

Begin as one Cargo package. Split crates only when reuse, dependency isolation, or build performance makes the boundary real.

```text
.
├── Cargo.toml
├── Cargo.lock
├── README.md
├── VISION.md
├── AGENTS.md
├── docs/
│   ├── MCP_INTERFACE.md
│   └── ARCHITECTURE.md
├── src/
│   ├── main.rs
│   ├── cli.rs
│   ├── config.rs
│   ├── error.rs
│   ├── mcp/
│   │   ├── mod.rs
│   │   ├── server.rs
│   │   ├── tools.rs
│   │   ├── console.rs
│   │   └── console_session.rs
│   ├── session/
│   │   ├── mod.rs
│   │   ├── id.rs
│   │   ├── manager.rs
│   │   ├── state.rs
│   │   ├── generation.rs
│   │   └── paths.rs
│   ├── worker/
│   │   ├── mod.rs
│   │   ├── main.rs
│   │   ├── process.rs
│   │   ├── supervisor.rs
│   │   ├── protocol.rs
│   │   └── control.rs
│   ├── runtime/
│   │   ├── mod.rs
│   │   ├── cell.rs
│   │   ├── input.rs
│   │   ├── display.rs
│   │   ├── r/
│   │   │   ├── mod.rs
│   │   │   ├── ffi.rs
│   │   │   ├── startup.rs
│   │   │   ├── eval.rs
│   │   │   ├── console.rs
│   │   │   ├── interrupt.rs
│   │   │   └── graphics.rs
│   │   ├── python/
│   │   │   ├── mod.rs
│   │   │   ├── bridge.rs
│   │   │   ├── cell.py
│   │   │   └── input.py
│   │   └── sql/
│   │       ├── mod.rs
│   │       ├── bridge.R
│   │       ├── connection.rs
│   │       ├── fetch.rs
│   │       └── register.rs
│   ├── environment/
│   │   ├── mod.rs
│   │   ├── manifest.rs
│   │   ├── resolver.rs
│   │   ├── r.rs
│   │   ├── python.rs
│   │   └── cache.rs
│   ├── output/
│   │   ├── mod.rs
│   │   ├── timeline.rs
│   │   ├── spool.rs
│   │   ├── preview.rs
│   │   ├── table.rs
│   │   └── response.rs
│   ├── transcript/
│   │   ├── mod.rs
│   │   ├── journal.rs
│   │   ├── model.rs
│   │   └── qmd.rs
│   └── sandbox/
│       ├── mod.rs
│       ├── policy.rs
│       ├── linux.rs
│       ├── macos.rs
│       ├── windows.rs
│       └── codex.rs
├── tests/
│   ├── common/
│   ├── interface/
│   ├── runtime/
│   ├── lifecycle/
│   ├── output/
│   └── sandbox/
├── fixtures/
└── scripts/
```

Embedded R, Python, and SQL helper source belongs next to the Rust adapter that owns its behavior. It is implementation code and must be tested and versioned like Rust code.

## 6. Process model

### 6.1 Supervisor

The supervisor is the long-lived MCP server and trusted control plane.

Responsibilities:

- speak MCP over stdio initially;
- expose and validate the two public tools;
- manage logical session names, states, and generations;
- prepare additive package requirements;
- launch workers under explicit sandbox policy;
- own command, sideband, control, stdout, and stderr channels;
- enforce wait, polling, cancellation, and output-budget behavior;
- write output spools, internal records, and QMD projections;
- report worker crashes without corrupting MCP stdout;
- reset, close, and retain session files according to policy.

The supervisor must never load user packages or native extensions.

### 6.2 Worker

The worker is the arbitrary-code boundary.

Responsibilities:

- initialize embedded R on its main thread;
- install R frontend callbacks;
- create a persistent R user environment and private console environment;
- dispatch complete cells to the R, Python, or SQL adapter;
- initialize reticulate and DuckDB lazily;
- route runtime input to the active evaluation;
- emit structured state, output, display, artifact, and completion events;
- cooperate with interrupt and shutdown;
- keep private bootstrap handles inaccessible to ordinary user code where practical.

The worker reports runtime facts. It does not decide MCP wording, response budgets, transcript prose, or file-retention policy.

### 6.3 Session generations

A logical session can outlive a worker incarnation.

```text
default / generation 1
default / generation 2   after reset
```

In-memory R, Python, SQL, debugger, and native-library state never crosses a generation boundary. Declared requirements and workspace files may persist. Evaluation IDs must remain unambiguous across generations.

## 7. Worker threading and event loop

R must be initialized and called from one owning worker thread, normally the worker main thread.

A practical design is:

```text
worker main thread
  owns R
  waits for accepted commands
  dispatches cells synchronously
  services R event processing as required

IPC reader/helper thread
  receives supervisor commands
  places evaluation and stdin messages on synchronized queues

control helper / OS signal path
  can request interrupt or termination while main thread is executing
```

The main thread is not R's terminal REPL loop. It is an MCP Console dispatch loop around an embedded R runtime. R's frontend callbacks remain installed for output, real console input, messages, busy state, and shutdown behavior.

## 8. Private worker protocol

The supervisor–worker protocol is private, versioned, framed, and stricter than the MCP schema. JSON Lines over dedicated pipes is sufficient for v1; large binary data belongs in files or a separate binary path.

### 8.1 Logical channels

```text
command:       supervisor -> worker
sideband:      worker -> supervisor
control:       supervisor -> worker or OS runtime interrupt
raw stdout:    worker and descendants -> supervisor
raw stderr:    worker and descendants -> supervisor
```

The control path must remain usable while the R-owning thread is blocked in evaluation.

### 8.2 Representative commands

```rust
enum WorkerCommand {
    Initialize {
        protocol_version: u32,
        session: SessionId,
        generation: u64,
        workspace: PathBuf,
        environment: ResolvedEnvironment,
    },
    EvaluateCell {
        evaluation_id: EvaluationId,
        language: Language,
        source: String,
        label: Option<String>,
    },
    ProvideInput {
        evaluation_id: EvaluationId,
        input_request_id: InputRequestId,
        text: String,
    },
    PrepareShutdown {
        reason: ShutdownReason,
    },
}
```

Evaluation source and interactive input never share a generic input queue.

### 8.3 Representative events

```rust
enum WorkerEvent {
    Ready { capabilities: Capabilities },
    EvaluationStarted { evaluation_id: EvaluationId, language: Language },
    OutputText { evaluation_id: EvaluationId, stream: Stream, bytes: Vec<u8> },
    DisplayValue { evaluation_id: EvaluationId, value: DisplayValue },
    ArtifactCreated { evaluation_id: EvaluationId, artifact: Artifact },
    InputRequested {
        evaluation_id: EvaluationId,
        input_request_id: InputRequestId,
        origin: InputOrigin,
        prompt: String,
        echo: bool,
    },
    InputConsumed { evaluation_id: EvaluationId, input_request_id: InputRequestId },
    EvaluationFinished { evaluation_id: EvaluationId, outcome: EvaluationOutcome },
    InterruptAcknowledged { evaluation_id: Option<EvaluationId> },
    SessionEnded { reason: EndReason, message: Option<String> },
    ProtocolWarning { message: String },
}
```

`DisplayValue` is a bounded or structured description, not an arbitrary serialized language object.

### 8.4 Completion rules

An evaluation is complete only after `EvaluationFinished` or worker termination. A quiet pipe, a familiar prompt, or a short settling delay is never sufficient evidence.

An `InputRequested` event suspends the initiating MCP wait but does not finish the evaluation.

Move exact message schemas and ordering constraints into `docs/WORKER_PROTOCOL.md` once implementation begins.

## 9. Runtime dispatch and stack semantics

The worker dispatches each accepted cell by language:

```rust
match cell.language {
    Language::R => r.evaluate_cell(cell),
    Language::Python => python.evaluate_cell(cell),
    Language::Sql => sql.evaluate_cell(cell),
}
```

There is no universal interpreted R call such as `.mcp_console_eval(id)` around every language.

The stack contract is intentionally asymmetric:

| Input | Initial implementation | Console-owned interpreted R frame |
|---|---|---:|
| R | native parse/evaluate loop | no |
| Python | one private R-to-reticulate bridge | yes, initially |
| SQL | one private R-to-DBI/DuckDB bridge | yes, initially |
| stdin | resume active input consumer | no new top-level frame |

This asymmetry follows the actual implementation boundaries and should be documented rather than concealed.

## 10. R runtime

### 10.1 Initialization

Initialize R once per worker generation using the same class of native frontend APIs used by Ark and `mcp-repl`.

The worker should:

- discover and configure `R_HOME` and library paths;
- initialize R as interactive but disable automatic workspace restore/save;
- honor or deliberately configure startup files and repositories;
- install `WriteConsoleEx`, `ReadConsole`, message, busy, callback, and shutdown hooks as appropriate per platform;
- initialize `harp` routines and any graphics/help integration;
- create a persistent user environment;
- create a private environment for console-owned state and bridge functions;
- process platform event hooks required by embedded R.

Cross-platform startup behavior is a major reason to build from `mcp-repl` and compare with Ark rather than beginning from raw `libR` examples.

### 10.2 Complete-cell evaluation

R source is not sent through `ReadConsole` as terminal lines.

For each R cell:

1. associate the source with a synthetic source name such as `<mcp-console:r:g1:e17>`;
2. parse the complete cell with source references retained where practical;
3. distinguish incomplete input from invalid syntax;
4. evaluate top-level expressions sequentially in the persistent user environment;
5. run each expression inside a native top-level error/interrupt boundary;
6. inspect R's visibility state and print visible values with console semantics;
7. emit conditions, errors, artifacts, and completion under the evaluation ID;
8. restore runtime hooks after recoverable errors or interrupts.

Earlier expressions in a multi-expression cell may have changed state before a later error. Do not pretend the whole cell is transactional.

Do not make `source()`, `withAutoprint()`, or `eval(str2expression(...))` the fundamental evaluator. They can add interpreted helper frames and make source, visibility, and error behavior harder to control. The target is a native frontend evaluator equivalent in spirit to what a real console does.

### 10.3 R call-stack contract

Because Rust invokes parsed expressions directly at a native boundary, ordinary R stack introspection should not contain a console-owned interpreted frame.

For example:

```r
f <- function() sys.calls()
f()
```

should show user R calls equivalent to `f()`, not an outer `.mcp_console_eval(...)`, `source(...)`, or `withAutoprint(...)` call.

Native error-catching functions and C/Rust frames are not represented by `sys.calls()` and are acceptable.

This contract requires direct integration tests because subtle evaluation helpers can change stack and traceback behavior.

### 10.4 `ReadConsole` and interactive input

`ReadConsole` is reserved for genuine runtime input:

- `readline()`;
- `browser()` and `recover()`;
- package code that reads from the R console;
- startup code that deliberately requests input, if supported.

When called during an active evaluation, the callback:

1. allocates an `InputRequestId`;
2. emits `InputRequested` with prompt and origin;
3. blocks until the matching `ProvideInput` arrives, interrupt occurs, or shutdown begins;
4. returns exactly that input line to R;
5. emits consumption bookkeeping.

It must not draw from a queue containing future source lines from the submitted cell.

## 11. Python runtime through reticulate

### 11.1 Initialization and persistent state

Python is initialized lazily after applicable requirements have been declared. Reticulate owns one interpreter and persistent Python `__main__` module inside R.

R code accesses Python objects through reticulate's `py` object. Python accesses R through reticulate's `r` object. Do not create another global namespace protocol unless a concrete interoperability gap requires it.

### 11.2 Cell evaluator

Do not use `py_eval()` for general cells; it accepts expressions, not assignments and statements. Do not use a generic line-fed `repl_python(input = ...)` loop as the fundamental cell transport, because nested `input()` or debugger reads must not consume remaining source lines.

Install a small Python cell executor that:

1. compiles the complete source using a synthetic filename such as `<mcp-console:python:g1:e18>`;
2. rejects incomplete parser input;
3. executes statements in persistent `__main__.__dict__`;
4. evaluates and displays a final expression when present;
5. preserves Python exceptions and traceback locations;
6. routes standard input and debugger reads through the active MCP Console input bridge;
7. leaves imports and globals persistent.

A common implementation is to parse the cell with Python's `ast` module, execute all but a final `Expr`, then evaluate and display that final expression. The exact helper belongs in `runtime/python/cell.py` and must be exercised through the public MCP interface.

### 11.3 R bridge and stack behavior

Reticulate's supported entry points are R APIs. A practical v1 can call one private R helper, for example conceptually:

```r
.mcp_console_private$eval_python(evaluation_id)
```

The source should be stored out of band under the evaluation ID rather than interpolated into the R call. The helper invokes the installed Python cell executor and returns only runtime-neutral results.

While Python is active, the R call stack therefore contains one console bridge plus reticulate frames. If Python calls an R function, raw `sys.calls()` in that callback may show those outer frames. This is truthful and acceptable.

Curated diagnostics may collapse known internal frames for readability, but raw R introspection must not be falsified. A later supported native reticulate entry point could remove the console-owned R helper without changing the MCP interface.

### 11.4 Python stdin and debuggers

Install a Python `sys.stdin` or `builtins.input` bridge that uses the same `InputRequested`/`ProvideInput` state machine as R. It should support at least ordinary `input()` and line-oriented debugger commands.

Do not assume that R's `ReadConsole` automatically provides correct Python stdin semantics. Verify `input()`, `pdb`, nested R callbacks, interruption, and EOF behavior in an implementation spike.

## 12. SQL runtime through DuckDB and DBI

### 12.1 Initial ownership

The initial SQL implementation uses the DuckDB R package and DBI in the same worker process. It does not launch the DuckDB CLI and does not implement a new SQL engine.

Create one connection per worker generation, conceptually:

```r
DBI::dbConnect(
  duckdb::duckdb(
    dbdir = ":memory:",
    environment_scan = TRUE,
    shared_home = FALSE
  )
)
```

Pin exact supported DuckDB arguments and versions in code and tests. Direct DuckDB storage, extension, and secret paths into session-controlled locations rather than ambient user state.

### 12.2 SQL bridge and R stack

A practical v1 calls one private R helper, conceptually:

```r
.mcp_console_private$eval_sql(evaluation_id)
```

The helper retrieves the SQL source from private state and calls DBI against the persistent connection.

While the SQL evaluation is active, an R callback or debugger may observe:

- the private SQL bridge frame;
- DBI generic and method frames;
- DuckDB R backend frames.

SQL itself has no `sys.calls()` equivalent tied to R. After the query returns, these R frames are gone. This is an explicit cost of using the R/DBI integration and should be covered by stack-behavior tests.

Do not place the entire SQL source literal in the helper call, where it could make `sys.calls()` and diagnostics unwieldy.

### 12.3 Why use the R connection first

The R integration provides the shortest path to useful shared state:

- DuckDB catalog state persists in the worker;
- R data frames can be discovered through environment scanning;
- `duckdb_register()` can expose R data frames without copying;
- `duckdb_register_arrow()` can expose Arrow-backed sources;
- Python objects can initially cross reticulate conversion or an Arrow bridge.

The adapter must deliberately arrange the evaluation environment used for environment scanning and test name precedence, rebinding, and object lifetime. Do not rely on accidental internal call frames.

### 12.4 Query execution and bounded fetching

The SQL adapter owns:

- execution of complete SQL cells;
- result-set versus statement behavior;
- bounded fetching and previews;
- affected-row or statement summaries;
- reliable result cleanup;
- SQL errors and source locations;
- relation registration helpers;
- cooperative interruption where the R backend permits it.

Use `dbSendQuery()`/`dbSendStatement()` plus bounded `dbFetch(n = ...)`, or DBI Arrow/record-batch APIs. Never call `dbGetQuery()` on arbitrary agent SQL when it can collect an unbounded result into R.

If backend-specific statement metadata is required, isolate all unstable DuckDB-R access behind one adapter and pin a compatibility test.

### 12.5 Relation visibility

V1 rules:

1. persistent DuckDB tables and views use ordinary SQL names;
2. when no catalog relation exists, environment scanning may resolve an R data frame from the intended user environment;
3. a runtime helper such as `sql_register(name, object)` explicitly publishes a stable relation;
4. R data frames use `duckdb_register()` where appropriate;
5. Arrow-backed R objects use `duckdb_register_arrow()`;
6. Python data frames initially pass through reticulate conversion or Arrow registration;
7. automatic scanning of all Python globals is deferred.

Document and test precedence between catalog relations, explicit registrations, and scanned R variables.

### 12.6 DuckDB CLI relationship

The CLI is a behavioral reference only. Borrow ideas such as:

- bounded duckbox-like previews;
- SQL-native discovery workflows;
- useful progress and interruption behavior;
- perhaps a previous-result relation in a later version.

Do not expose dot commands, terminal modes, line-oriented continuation, shell escapes, mutable output redirection, or the CLI executable as the SQL transport.

### 12.7 Possible future native DuckDB ownership

Rust could later own DuckDB directly through its C or Rust API. That would remove SQL's R bridge and improve access to progress, interruption, data chunks, and result metadata.

It would also lose automatic R environment scanning and require an explicit Arrow or table-function bridge with careful R object lifetime management. Adopt native ownership only after measurements show that the R/DBI boundary is the limiting factor.

## 13. Runtime helper API

Prefer in-language helpers over more MCP tools. Possible R helpers include:

```r
sql_query(sql)             # deliberately collect a SQL result into R
sql_exec(sql)              # execute a SQL statement
sql_register(name, x)      # publish a relation
sql_unregister(name)
sql_tables()
console_transcript()
console_artifact_path(name)
```

Python can call them through reticulate's `r` object. These helpers are a runtime API and should eventually receive focused documentation and compatibility tests.

## 14. Dependency architecture

Each logical session owns an additive manifest:

```rust
struct EnvironmentManifest {
    r: BTreeSet<String>,
    python: BTreeSet<String>,
}
```

The supervisor or a separate restricted resolver prepares requirements before code starts. Package download access must not require granting general network access to the arbitrary-code worker.

### 14.1 Python

Use reticulate's managed environment and `py_require()` semantics where practical. Requirements must be finalized before first Python initialization whenever they constrain interpreter choice. After initialization, v1 permits additive requirements only.

### 14.2 R

Use a configured R resolver and library cache with equivalent additive behavior. The exact package-reference grammar belongs in a later `docs/DEPENDENCIES.md`.

### 14.3 Atomic public behavior

For a call containing requirements and code:

1. merge the requirements;
2. resolve and activate the environment;
3. launch or update the worker as permitted;
4. execute the cell only after preparation succeeds.

If preparation fails, no part of the cell runs.

## 15. Output architecture

### 15.1 Sources

Output can arrive through:

- R `WriteConsoleEx` and message hooks;
- Python stdout/stderr and display hooks;
- SQL result/display events;
- graphics or artifact callbacks;
- raw worker or child-process file descriptors.

Managed hooks provide evaluation attribution. Raw pipes are the fallback for native libraries and descendants that bypass language hooks.

### 15.2 Per-evaluation spool

Each evaluation owns an append-only output spool. Preserve complete explicit stream output when practical, including output omitted from MCP replies.

The supervisor owns a reply cursor per active evaluation. A response snapshots output from the cursor to the current end, applies a global budget, writes a truncation marker and path if needed, then advances the cursor to that end.

The cursor advances past omitted bytes. Future polls return new activity rather than requiring textual pagination through an old flood.

### 15.3 Ordering

Managed events and raw file-descriptor output can race. Define one visible output timeline with monotonic sequence numbers assigned at ingestion. Do not claim impossible byte-perfect ordering across independent OS streams.

If a PTY merges stdout and stderr, preserve the merged truth rather than inventing stream identity.

### 15.4 Structural previews

Recognized values should become a runtime-neutral `DisplayValue` before final rendering:

```rust
enum DisplayValue {
    Text(String),
    Table(TablePreview),
    Array(ArrayPreview),
    Mapping(MappingPreview),
    Artifact(Artifact),
    Summary(ObjectSummary),
}
```

A preview must already be bounded in rows, columns, elements, and cell widths. The supervisor then applies the final response-byte budget.

Unknown classes may invoke arbitrary print methods. Capture that output to the spool and truncate it normally rather than pretending it is safely previewable.

### 15.5 SQL previews

Fetch only `preview_rows + 1` or enough record batches to determine that additional rows exist. Do not count an entire arbitrary relation solely to print an exact total. Report exact dimensions only when cheaply available from metadata or a deliberate count.

### 15.6 Plots and artifacts

Save plots and binary artifacts beneath the session directory and emit relative paths. The MCP response remains text-only. The transcript links to the same files.

## 16. Session records and transcript

### 16.1 Directory layout

```text
.mcp-console/sessions/default/
├── transcript.qmd
├── environment.json
├── outputs/
│   ├── g1-e0017.log
│   └── g1-e0018.log
├── artifacts/
│   └── g1-e0021-plot-1.png
└── internal/
    └── events.jsonl
```

Use sanitized or encoded path components rather than trusting a logical session name as a path.

### 16.2 Internal journal

The internal journal is the authoritative durable event record for reconstruction and diagnostics. JSONL is a reasonable v1 format because it is append-friendly and can discard a partial final line after a crash.

It may record granular events such as:

```text
session_started
evaluation_accepted
evaluation_started
stream_appended
artifact_created
input_requested
input_supplied
evaluation_finished
worker_stopped
generation_started
```

This file is not the normal agent-facing artifact and need not expose Rust's internal serialization directly. Give it an explicit private schema version.

### 16.3 Generated QMD

`transcript.qmd` is the compact human- and agent-facing projection. Update it at stable boundaries: completion, error, interruption, input request, worker stop, and generation change.

It contains complete source, labels, bounded output, errors, supplied input when safe, and relative artifact paths. It is marked non-executing and generated.

Do not continuously embed unlimited output. Refer to full output sidecars when excerpts are insufficient.

Do not let agents edit the live generated transcript in place. Refined notebooks, reports, and scripts are separate files created from the transcript and runtime artifacts.

## 17. Session manager and concurrency

The supervisor owns the state machine:

```text
absent -> preparing -> starting -> idle -> running
                                      running -> input_required -> running
                                      running -> idle
                                      running -> stopped
                                      stopped -> starting   on reset
                                      stopped -> absent     on close
```

One session accepts one active evaluation. Independent parallel work uses separate named sessions and therefore separate worker processes.

Multiple MCP poll requests may wait on the same evaluation, but output cursor semantics must be defined so one waiter cannot cause another to receive an incoherent replay. The simplest v1 policy is one active consumer/waiter per session; if multiple waiters are allowed, give each its own observation cursor.

Do not automatically evict local stdio sessions in v1 unless measured resource pressure requires it.

## 18. Interrupt, reset, and failure

### 18.1 Interrupt

Interrupt is cooperative and runtime-aware. It must bypass the ordinary evaluation queue. The implementation may combine:

- a helper thread setting R interrupt state;
- an OS signal where safe;
- Python interrupt signaling;
- DuckDB connection interruption where exposed.

After interruption, the worker reports whether it recovered to idle. Do not silently reset if cooperative recovery fails.

### 18.2 Reset

Reset terminates the worker, increments the generation, and starts a fresh worker with the retained dependency manifest and workspace policy. It destroys all in-memory state.

### 18.3 Crash

A segfault, abort, OOM kill, or unrecoverable embedded-runtime failure marks the session stopped. Preserve the transcript and output produced before death. Do not restart automatically and imply state continuity.

## 19. Sandbox and security

MCP Console provides shell-class capability. Enforce policy around the entire worker process:

- read and write roots;
- network access;
- subprocess inheritance;
- environment and secret forwarding;
- CPU, memory, process-count, file-size, and output limits;
- extension and native-library loading consequences;
- package resolver access.

The supervisor remains trusted and should have a small dependency surface. Worker descendants must inherit restrictions.

Where Codex provides per-call sandbox metadata, the supervisor may derive worker policy from it. Missing or malformed inherited policy must fail closed. Platform details belong in a future `docs/SANDBOX.md`.

## 20. MCP implementation

Use the official Rust MCP SDK (`rmcp`) unless a concrete gap appears. Keep it behind a thin adapter:

```text
mcp/
  deserialize and validate public arguments
  call session services
  translate results into bounded text and isError

session/
  own product state and semantics

worker/ and runtime/
  own process and interpreter mechanics
```

The public MCP protocol supports richer content, but v1 deliberately uses plain text plus workspace files because that is the most predictable contract for the target client.

## 21. Error model

### Tool errors (`isError: true`)

- invalid mode combinations;
- code sent while busy;
- `stdin` while no input is pending;
- missing session for poll/control;
- dependency preparation failure;
- worker startup or protocol failure;
- sandbox setup or authorization failure.

### Language outcomes (`isError: false`)

- R parse errors and conditions;
- Python exceptions;
- SQL errors;
- recoverable interrupts.

The session returns to idle only if the runtime reports successful recovery.

### Fatal runtime failures

Worker process death produces a stopped session and an infrastructure diagnostic. It is never formatted as an ordinary language error.

## 22. Testing strategy

### 22.1 Public integration tests

Build the binary, launch it as an MCP stdio server, and test real tool calls. Required scenarios include:

- minimal R, Python, and SQL cells;
- persistent state across alternating languages;
- R-to-Python and Python-to-R access;
- R data frame queried in SQL;
- lazy session creation and named-session isolation;
- code rejected while busy;
- wait expiry without cancellation;
- R `readline()` and `browser()`;
- Python `input()` and debugger input;
- interrupt, reset, close, and crash behavior;
- additive requirements;
- bounded stdout/stderr and sidecars;
- bounded R, Python, and SQL values;
- plot paths;
- QMD transcript recovery;
- sandbox restrictions.

### 22.2 Stack-semantics tests

Explicitly test:

- R `sys.calls()` does not include a console-owned wrapper for an R cell;
- R tracebacks retain useful synthetic source references;
- Python-to-R callbacks show the documented bridge boundary;
- SQL-to-R callbacks, where possible, show the documented DBI boundary;
- source text is not duplicated into internal stack calls.

### 22.3 Fake-worker tests

Use a deterministic fake worker for races and malformed sequences:

- output before ready;
- input request after partial output;
- output flood between polls;
- interrupt during input;
- malformed event;
- crash with trailing bytes;
- cancellation races;
- partial internal journal records.

### 22.4 Snapshot discipline

Avoid snapshots tied to terminal width, ANSI color, absolute temporary paths, package download progress, nondeterministic ordering, or exact internal traceback frames across runtime patch versions.

## 23. Implementation sequence

### Milestone 0: contract and fake worker

- implement `rmcp` stdio server;
- expose the two draft schemas;
- implement validation and session state types;
- build a fake worker and end-to-end harness.

Exit: all public states and lifecycle actions work without real R.

### Milestone 1: native R worker

- base startup on `mcp-repl`/Ark low-level patterns;
- initialize R on the worker main thread;
- evaluate complete R cells directly;
- capture output and interactive `ReadConsole` requests;
- implement poll, interrupt, reset, and crash reporting;
- validate R stack semantics.

Exit: persistent R, visible values, `readline()`, `browser()`, oversized output, and reset work through MCP.

### Milestone 2: output and transcript

- add output spools and reply cursors;
- add structural previews;
- add internal journal and generated QMD;
- add plot/artifact files.

Exit: no tested reply exceeds its configured budget and the QMD reconstructs useful session history.

### Milestone 3: reticulate Python

- prepare Python requirements before initialization;
- install persistent Python cell and stdin bridges;
- support final-expression display and tracebacks;
- test Python input, debugger behavior, R callbacks, and stack contract.

Exit: alternating R and Python cells share state in one worker.

### Milestone 4: DuckDB SQL

- initialize persistent DuckDB through R/DBI;
- add private SQL bridge and bounded result fetching;
- enable and test R environment scanning;
- add explicit R/Arrow registration and Python conversion path;
- test SQL stack boundary and output limits.

Exit: data loaded in Python can be converted or registered, queried in SQL, and consumed from R or Python.

### Milestone 5: environment manager

- add additive R/Python manifests;
- add resolution caches and provenance;
- separate resolver network policy from worker policy.

Exit: requirements and code form one atomic public operation.

### Milestone 6: sandbox and platform hardening

- implement macOS, Linux, and Windows policies;
- inherit host policy where supported;
- add resource quotas and escape tests;
- harden private IPC and session paths.

Exit: supported-platform security and resource tests pass in CI.

## 24. Required implementation spikes

1. **R dispatch loop:** prove direct complete-cell evaluation while retaining correct event processing, visible values, interrupts, and `ReadConsole` behavior on all target platforms.
2. **Ark/mcp-repl reuse:** choose pinned crates, extraction, or vendoring and document upgrade responsibility.
3. **R stack semantics:** validate `sys.calls()`, traceback, source references, errors, and `browser()` under native evaluation.
4. **Python cell evaluator:** compare a custom AST helper with reticulate internals; verify final-expression display without line-queue ambiguity.
5. **Python stdin:** verify `input()`, `pdb`, EOF, interrupt, and callbacks into R.
6. **DuckDB environment scan:** verify the exact R environment used, name precedence, rebinding, and registration lifetimes.
7. **DuckDB bounded fetch:** compare bounded DBI, Arrow, and record-batch paths and confirm interruption behavior.
8. **Output ordering:** define the merge contract for managed events and raw process streams.
9. **Transcript recovery:** choose incremental QMD updates versus deterministic rebuild on startup.
10. **Cancellation:** define exact mapping between MCP cancellation, initiating calls, later poll waiters, and runtime interrupts.

Record resolved spikes as short ADRs and update this document rather than leaving contradictory alternatives in place.

## 25. External references

- MCP tools specification: <https://modelcontextprotocol.io/specification/2025-11-25/server/tools>
- Official Rust MCP SDK: <https://github.com/modelcontextprotocol/rust-sdk>
- `mcp-repl`: <https://github.com/posit-dev/mcp-repl>
- Ark: <https://github.com/posit-dev/ark>
- Reticulate: <https://rstudio.github.io/reticulate/>
- `py_run_string()`: <https://rstudio.github.io/reticulate/reference/py_run.html>
- `repl_python()`: <https://rstudio.github.io/reticulate/reference/repl_python.html>
- `py_require()`: <https://rstudio.github.io/reticulate/reference/py_require.html>
- DuckDB R client: <https://duckdb.org/docs/current/clients/r>
- DuckDB R connection and environment scan: <https://r.duckdb.org/reference/duckdb.html>
- DuckDB data-frame registration: <https://r.duckdb.org/reference/duckdb_register.html>
- DBI query lifecycle: <https://dbi.r-dbi.org/reference/dbSendQuery.html>
- Quarto Markdown: <https://quarto.org/docs/authoring/markdown-basics.html>
