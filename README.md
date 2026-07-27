# MCP Console

MCP Console is a persistent, sandboxed computational workbench for AI agents. One console session hosts R, Python, and SQL in a single process, allowing an agent to load data once and use whichever language is best for each step.

> **Status:** design-stage repository. The interface and architecture are drafts intended to scaffold implementation.

## Proposed experience

The common interaction is deliberately small:

```json
{"python":"import json\nlogs = json.load(open('logs.json'))"}
```

```json
{"r":"df <- tibble::as_tibble(py$logs)"}
```

```json
{"sql":"select level, count(*) as n from df group by level"}
```

```json
{}
```

The first three calls evaluate complete cells in a persistent shared session. The empty call waits for new output or completion of a long-running evaluation.

When running code requests real console input, the same tool supplies `stdin`:

```json
{"stdin":"where"}
```

This supports R `readline()` and `browser()`, Python `input()` and debuggers, and similar interactive modes without making normal code submission line-oriented.

## Runtime model

Each named session is a sandboxed worker process:

```text
Rust MCP supervisor
  └── session worker
        └── embedded R
              ├── persistent R environment
              ├── reticulate Python interpreter
              └── persistent DuckDB connection
```

R is the host runtime. Python is embedded through reticulate. SQL is initially executed through the DuckDB R package and DBI, giving SQL direct access to live R data frames and persistent DuckDB catalog state.

The worker uses a small private protocol specialized for cell evaluation, output, interactive input, and control. It does not run Ark as a kernel and does not use the Jupyter wire protocol. The implementation should reuse the lower-level R integration work in `harp`/`libr` and the current `mcp-repl` runtime where practical.

## Output and durable context

MCP results are text-only and strictly bounded. Large values receive structural previews, while complete explicitly printed output is retained in session files. Every session maintains a generated `transcript.qmd` containing submitted code, bounded output, errors, labels, and artifact paths.

The Quarto transcript is a chronological execution record, not a polished notebook. An agent can use ordinary file tools to turn selected work into a refined `.qmd`, `.R`, `.py`, or `.ipynb` artifact.

## Documents

- [`VISION.md`](VISION.md) — product purpose, design goals, non-goals, and success criteria.
- [`docs/MCP_INTERFACE.md`](docs/MCP_INTERFACE.md) — proposed MCP tools and normative observable behavior.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — process model, runtime internals, output and transcript design, testing strategy, and implementation plan.
- [`AGENTS.md`](AGENTS.md) — durable project context, key decisions, repository sitemap, and rules for coding agents.

## Core decisions

- Product name: **MCP Console**.
- Public abstraction: a persistent console session, not a notebook or a conventional line-oriented REPL.
- Top-level input: complete R, Python, or SQL cells.
- Interactive input: explicit `stdin` only when the active runtime requests it.
- Public surface: `console` plus a low-frequency `console_session` control tool.
- Language selection: the object key is `r`, `python`, or `sql`.
- Runtime substrate: custom worker on `harp`/`libr`; no Ark process and no internal Jupyter protocol.
- R evaluation: native top-level evaluation; top-level cells are not transported through `ReadConsole`.
- SQL engine: embedded DuckDB through R/DBI initially; the DuckDB CLI is a behavioral reference only.
- Output: bounded MCP text plus ordinary workspace files.
- Durable record: generated Quarto transcript; granular JSONL journal remains internal.
- Isolation: one sandboxed worker process per named session.

## Upstream foundations

The design builds on:

- [posit-dev/mcp-repl](https://github.com/posit-dev/mcp-repl) for persistent worker, sandbox, output, and native R frontend patterns;
- [posit-dev/ark](https://github.com/posit-dev/ark), especially `harp` and `libr`, for lower-level R integration and as a reference for native R frontend behavior;
- [reticulate](https://rstudio.github.io/reticulate/) for embedded Python and R/Python object interchange;
- [DuckDB](https://duckdb.org/docs/current/clients/r) and [DBI](https://dbi.r-dbi.org/) for embedded SQL;
- [Quarto](https://quarto.org/) for the readable session transcript.
