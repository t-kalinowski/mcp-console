# MCP Console Vision

MCP Console is a persistent, sandboxed computational workbench for AI agents. A console session hosts R, Python, and SQL in one process: R is the host runtime, Python is embedded through reticulate, and SQL is backed by a persistent DuckDB connection. The agent can load data once, move among the three front ends, inspect and transform live state, and continue across tool calls.

The product is a **console**, not a shell wrapper, a conventional line-oriented REPL, or a notebook kernel. Normal work is submitted as complete R, Python, or SQL cells. Real line-oriented input appears only when the running program requests it, including `readline()`, `input()`, `browser()`, and debuggers.

## Why this should exist

A shell can launch R or Python, but a fresh process discards the objects, imports, packages, debugger state, and context established by the previous turn. Separate language tools preserve some state but make interchange explicit and file-oriented. A notebook preserves cells and outputs, but it is primarily a document abstraction and carries kernel, MIME, editing, and execution-order conventions that are unnecessary for a high-frequency agent tool.

MCP Console should make exploratory data work feel like a conversation with one live computational environment. The agent should choose the most useful language for each step without repeatedly reconstructing state or flooding its context with runtime details.

## Design goals

### 1. Cheap enough to keep globally enabled

The tool definitions, common calls, and ordinary replies must remain small. A broadly useful capability loses much of its value when every conversation pays for a large schema or verbose response envelope.

### 2. One persistent environment with three first-class front ends

R, Python, and SQL should share one process, workspace, dependency context, and explicit object bridges. The agent should be able to load data in Python, manipulate it in R, and query it in SQL without starting another process or serializing through user-managed files.

### 3. Native language behavior

Each frontend should behave like its own language rather than a string hidden inside another language's visible wrapper. R cells should have top-level R evaluation semantics. Python should preserve persistent `__main__`, language-native tracebacks, and final-expression display. SQL should expose DuckDB diagnostics and persistent catalog state. Necessary R/Python or R/SQL bridge frames should be minimal and truthful rather than disguised.

### 4. Cell-oriented evaluation with real console interactivity

Top-level code is submitted as a complete cell. Incomplete parser input is an error rather than an invitation to continue a program over several MCP calls. When evaluated code explicitly asks for input or enters a debugger, the session switches to an input state and accepts line-oriented `stdin`.

### 5. Exact runtime state, never prompt inference

The server must know whether a session is preparing, starting, idle, running, waiting for input, stopped, or absent. State transitions come from structured runtime events. Prompts such as `>`, `...`, and `Browse[1]>` are output, not protocol.

### 6. Fast synchronous turns with bounded waiting

Most evaluations should complete in the initiating tool call. A wait limit ends only that MCP request; it does not terminate the computation. A later empty call waits for new output or completion. Ordinary work should not require a polling ceremony, while long work remains observable and interruptible.

### 7. Useful output without context flooding

Small results appear directly. Large values receive bounded structural previews before full stringification. Tables are limited by rows, columns, cell width, and total reply size. Explicit stdout and stderr are retained completely in session files, while each MCP reply contains only a bounded current excerpt.

### 8. Durable context outside the conversation

Each session maintains a generated `transcript.qmd` containing submitted code, labels, bounded output, errors, input interactions, and artifact paths. After context compaction, the agent can recover what happened using ordinary file tools.

The transcript is a chronological execution record, not a polished or necessarily reproducible notebook. Refined `.qmd`, `.R`, `.py`, or `.ipynb` artifacts are created separately. A granular event journal may support recovery internally, but it is not the default agent-facing artifact.

### 9. Persistent, explicit session environments

R and Python requirements are relatively infrequent session configuration, not modifiers on ordinary code cells. The agent prepares additive requirements through the session-control tool. Requirements survive runtime restarts, while R objects, Python objects, loaded imports, debugger state, and the in-memory DuckDB catalog do not. Environment changes must be explicit and must never silently destroy live state.

### 10. Explicit and predictable interoperability

Language selection is encoded by the input key: `r`, `python`, or `sql`. The server never guesses from source text. R and Python use reticulate's object bridge. SQL uses persistent DuckDB state, live R relation discovery where safe, and explicit registration for stable R, Python, or Arrow-backed relations.

### 11. Honest lifecycle and failure behavior

A session runs at most one top-level evaluation at a time. Interrupt attempts to preserve state. Restart deliberately starts a new runtime generation and loses in-memory state while retaining session requirements, workspace files, and transcript. A crashed worker remains stopped until explicitly restarted or closed; the server never silently replaces it and implies that state survived.

### 12. Process-level safety

MCP Console is effectively an ergonomic shell. Arbitrary R and Python can access files, native libraries, subprocesses, and networks when permitted. Safety therefore belongs around the worker process and its descendants: filesystem policy, network policy, resource limits, secret isolation, and explicit host approval.

### 13. A narrow public surface over extensible internals

New capabilities should usually appear as ordinary language code, runtime helpers, files, or private protocol extensions rather than additional globally visible MCP tools. Every new public field or tool has a permanent model-context cost.

## Product model

- Product and binary: `mcp-console`.
- Frequent tool: `console`.
- Low-frequency environment and lifecycle tool: `console_session`.
- Session: one sandboxed worker process containing R, reticulate Python, and DuckDB.
- Submission: one complete R, Python, or SQL cell.
- Evaluation: execution of one submission.
- `stdin`: exact stream text for an already-running interactive consumer, not another code submission; it may contain multiple lines and receives no implicit newline.
- Durable record: generated `transcript.qmd` plus retained output and artifact files.
- Refined notebook, report, or script: a separate user artifact.

## Non-goals

The initial design does not aim to provide:

- a general terminal or PTY tool;
- a Jupyter kernel or notebook editing protocol;
- Ark as the runtime worker;
- line-by-line construction of top-level programs;
- automatic language inference;
- a variable or package inventory on every turn;
- automatic package installation after observing an import error;
- per-cell package requirements;
- arbitrary package removal or environment downgrades in a live session;
- automatic registration of every Python object as a SQL table;
- textual pagination through an entire large table;
- guaranteed zero-copy transfer for every object type;
- MCP structured output, resource retrieval, or inline images in v1;
- silent worker restart after state loss.

## Success criteria

The design is successful when:

1. Normal use reads like `{"python":"..."}`, `{"r":"..."}`, `{"sql":"..."}`, and `{}`.
2. The server exposes no more than two compact MCP tools.
3. Switching languages does not create another process or require a file round trip.
4. Ordinary evaluations finish in one call; long evaluations remain observable and interruptible.
5. R top-level evaluation does not introduce a console-owned R wrapper frame.
6. Debuggers and runtime input work without making normal code line-oriented.
7. No evaluation can inject unbounded text into model context.
8. After context compaction, the agent can inspect `transcript.qmd` and recover its work.
9. A worker crash cannot corrupt MCP transport or masquerade as preserved state.
10. The public behavior is testable end-to-end independently of implementation internals.
11. A contributor can understand the product contract and locate subsystem ownership from `AGENTS.md`.
