# MCP Console

MCP Console is a persistent, sandboxed R, Python, and DuckDB SQL console for AI agents.
One console session hosts R, Python, and SQL in a single process, allowing an agent to load data once and use whichever language is best for each step.

> **Status:** design-stage repository.
> The interface and architecture are drafts intended to scaffold implementation.

## Installation and registration

The intended MCP server command is:

```bash
uvx mcp-console serve
```

The PyPI package will contain platform wheels with the standalone Rust binary and a minimal Python launcher.
A persistent installation can use `uv tool install mcp-console`.
The MCP initialization identity remains `mcp-console`, while the intended default client registration name is `console`.
For an installed binary, Codex registration is:

```bash
codex mcp add console -- mcp-console serve
```

Under Codex's current naming convention, the tools are `mcp__console.send` and `mcp__console.session`.

`mcp-console` requires a subcommand.
`mcp-console serve` starts the MCP stdio server.
Additional commands diagnose the runtime, register supported MCP clients, and attach human-facing viewers to an already-running server.
See [`docs/CLI.md`](docs/CLI.md).

## Proposed agent experience

The common interaction is deliberately small:

```json
{ "python": "import json\nlogs = json.load(open('logs.json'))" }
```

```json
{ "r": "df <- tibble::as_tibble(py$logs)" }
```

```json
{ "sql": "select level, count(*) as n from df group by level" }
```

```json
{}
```

The first three calls evaluate complete cells in a persistent shared session.
The empty call waits for new output or completion of a long-running evaluation.
Used this way, the console acts as a computational workbench: the agent keeps live state in one place and reaches for R, Python, or SQL as needed.

When running code requests real console input, the same tool supplies exact `stdin` bytes:

```json
{ "stdin": "where\nn\nc\n" }
```

Input may also accompany the cell when it is already known:

```json
{ "r": "readline('name> ')", "stdin": "Ada\n" }
```

The text may contain one or more lines, and the server does not add a newline.
This supports R `readline()` and `browser()`, Python `input()` and debuggers, and similar interactive modes without making ordinary code submission line-oriented.
An input request is held briefly for a matching runtime receipt, so prequeued input can satisfy the read without forcing another tool call.
The receipt identifies the read operation, not the submitted payload that supplied its bytes.

Package requirements are configured less frequently at the logical-session level and persist across runtime restarts:

```json
{
  "action": "prepare",
  "requirements": { "r": ["dplyr"], "python": ["polars>=1"] }
}
```

`prepare` may create a configured logical session without starting its worker.
A later code cell or nonempty stdin submission starts the runtime with those requirements.
Once a runtime exists, this replaces it while retaining requirements, workspace files, and the transcript:

```json
{ "action": "restart" }
```

## Human visibility without a daemon

The MCP stdio process also owns a local, process-scoped API for human viewers and third-party sidecars.
It starts and stops with the MCP server; it never detaches, viewers do not keep it alive, and sessions do not survive the owning server process.

Representative commands are:

```bash
mcp-console list
mcp-console view default
mcp-console watch default
mcp-console send default --r 'summary(df)'
```

A viewer can follow evaluations and bounded live output, read the Markdown transcript, display plots at their original resolution, inspect supported objects, and browse large tables without placing their contents in model context.
A live view retrieves only the visible rows and columns from a retained runtime object; a snapshot view materializes a point-in-time relation that remains browsable while the agent continues computing.
Ephemeral filters and sorts can be converted back to R, Python, or SQL source without executing them invisibly.
The event stream is resumable and slow viewers cannot block evaluation.

The sidecar API separates three operation classes:

- **Observe:** read supervisor-owned state, transcript, outputs, and artifacts without entering the runtime.
- **Inspect:** issue bounded typed operations such as listing objects, opening a live table view, or materializing a snapshot.
  The caller supplies no arbitrary R or Python source.
- **Control:** submit code, stdin, or lifecycle changes.
  Arbitrary external code is attributed, enters the transcript, and is reported to the agent; there is no invisible “read-only code” channel.

R and embedded Python are owned by one runtime thread, so live-object requests are normally accepted only while the session is idle.
Live views avoid eagerly materializing very large objects and can reflect explicit revisions of the current binding.
Snapshot views use immutable Arrow, Parquet, or DuckDB-backed data so sorting, filtering, profiling, and paging can continue outside the live runtime while the agent computes.
See [`docs/SIDECAR_API.md`](docs/SIDECAR_API.md).

## Runtime model

Each named session is a sandboxed worker process:

```text
Rust MCP supervisor
  ├── MCP stdio adapter
  ├── process-scoped local viewer API
  └── session worker
        └── embedded R
              ├── persistent R environment
              ├── reticulate Python interpreter
              └── persistent DuckDB connection
```

R is the host runtime.
Python is embedded through reticulate.
SQL is initially executed through the DuckDB R package and DBI, giving SQL direct access to live R data frames and persistent DuckDB catalog state.

The supervisor exposes one backend-neutral runtime service for cell evaluation, output, interactive input, structured inspection, and control.
An Ark-backed R-only prototype was evaluated against the implemented purpose-built worker based on `harp`, `libr`, and libR's DLL REPL API.
The native worker was selected for the current text R slice; the broader R/Python/SQL and inspection backend remains open.
The public MCP and local sidecar contracts must not depend on which backend is selected.
See [`docs/RUNTIME_BACKEND.md`](docs/RUNTIME_BACKEND.md) and [`docs/R_REPL_DLL_ITERATOR.md`](docs/R_REPL_DLL_ITERATOR.md).

## Output and durable context

MCP results are text-only and strictly bounded.
Large values receive structural previews, while complete explicitly printed output is retained in session files.
Every session maintains a generated `transcript.md` containing submitted code, bounded output, errors, labels, origins, and artifact paths.
It also maintains a source-only `transcript.qmd` containing executable submitted code cells in call order and IR front matter for the session's declared R and Python requirements.

The Markdown transcript is a chronological execution record, not a polished report.
An agent can render the source-only QMD to reproduce the analysis incrementally, or use ordinary file tools to turn selected work into a refined `.qmd`, `.R`, `.py`, or `.ipynb` artifact.

The human-facing event stream carries bounded metadata and offsets, not unbounded tables or output.
Complete text, plots, live table batches, and snapshots are fetched separately by managed IDs.

## Documents

- [`VISION.md`](VISION.md) — product purpose, design goals, non-goals, and success criteria.
- [`docs/MCP_INTERFACE.md`](docs/MCP_INTERFACE.md) — proposed MCP tools and normative observable behavior.
- [`docs/TOOL_DESCRIPTIONS.md`](docs/TOOL_DESCRIPTIONS.md) — exact descriptions registered for the two MCP tools.
- [`docs/CLI.md`](docs/CLI.md) — standalone binary, installation, diagnostics, viewer, watch, and sidecar-control commands.
- [`docs/SIDECAR_API.md`](docs/SIDECAR_API.md) — process-scoped local API, event subscriptions, inspection boundary, data explorer, plots, and external evaluation semantics.
- [`docs/RUNTIME_BACKEND.md`](docs/RUNTIME_BACKEND.md) — initial Ark-versus-native evaluation, remaining full-runtime work, and decision criteria.
- [`docs/R_REPL_DLL_ITERATOR.md`](docs/R_REPL_DLL_ITERATOR.md) — native DLL-REPL findings, decision, and implementation record.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — process model, runtime internals, output, viewer architecture, testing strategy, and implementation plan.
- [`AGENTS.md`](AGENTS.md) — durable project context, key decisions, repository sitemap, and rules for coding agents.

## Core decisions

- Product name: **MCP Console**.
- Public abstraction: a persistent console session, not a notebook or conventional line-oriented REPL.
- Top-level input: complete R, Python, or SQL cells.
- Interactive input: exact, optionally multiline `stdin` text queued to the session worker whether it is evaluating or idle; paired request and receipt events expose supported runtime reads without gating delivery.
- MCP surface: `send` plus a low-frequency `session` environment and lifecycle tool.
- Language selection: the object key is `r`, `python`, or `sql`.
- Runtime substrate: the current text R console uses the purpose-built `harp`/`libr` worker; the broader backend decision remains open.
  Hide any backend behind the same runtime service.
- R evaluation: native top-level evaluation; complete cell source and worker stdin remain distinct streams.
- SQL engine: embedded DuckDB through R/DBI initially; the DuckDB CLI is a behavioral reference only.
- Output: bounded MCP text plus managed workspace files.
- Environment: additive session requirements configured by `session`; they survive runtime restarts.
- Lifecycle: `restart` replaces in-memory runtime state while retaining requirements, workspace files, and transcript.
- Durable record: generated Markdown transcript plus a source-only Quarto document; granular JSONL journal remains internal.
- Human visibility: a process-scoped local API with snapshot plus resumable event-stream semantics; no detached daemon.
- Sideband boundary: typed bounded inspection is distinct from arbitrary external evaluation.
- Data explorer: typed live views for bounded viewport access plus immutable snapshots for concurrent and repeatable exploration.
- Isolation: one sandboxed worker process per named session.

## Design influences

The design builds on:

- [posit-dev/mcp-repl](https://github.com/posit-dev/mcp-repl) for persistent worker, sandbox, output, and native R frontend patterns;
- [posit-dev/ark](https://github.com/posit-dev/ark) for native R execution, Jupyter lifecycle, plots, help, debugging, Variables, and the Data Explorer comm/backend; `harp` and `libr` remain candidate lower-level building blocks for a custom worker;
- [reticulate](https://rstudio.github.io/reticulate/) for embedded Python and R/Python object interchange;
- [DuckDB](https://duckdb.org/docs/current/clients/r) and [DBI](https://dbi.r-dbi.org/) for embedded SQL;
- [IR](https://github.com/r-lib/ir) and [Quarto](https://quarto.org/) for the source-only code-cell projection, managed requirements, and static report export;
- [kata](https://github.com/kenn-io/kata) for the pattern of one local service API shared by agent and human clients, protected runtime discovery, and resumable bounded event subscriptions;
- [Positron's Data Explorer](https://positron.posit.co/data-explorer.html) and [Plots pane](https://positron.posit.co/plots-pane.html) for human-facing ephemeral data exploration and full-scale plot inspection.
