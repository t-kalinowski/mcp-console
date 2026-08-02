# Runtime backend evaluation

**Status:** Open decision; required implementation spike \
**Date:** 2026-07-27 \
**Companion documents:** [`ARCHITECTURE.md`](ARCHITECTURE.md), [`SIDECAR_API.md`](SIDECAR_API.md), [`MCP_INTERFACE.md`](MCP_INTERFACE.md)

## 1. Decision to make

MCP Console needs one sandboxed worker process per named session.
The unresolved question is which component should own the R frontend and runtime event loop:

1. run **Ark** as the worker and adapt its Jupyter/kernel and custom-comm interfaces;
2. run a **purpose-built native worker** derived from `mcp-repl` and built on `harp`/`libr`;
3. extract or upstream a **shared lower-level R runtime** used by Ark and MCP Console.

This is an implementation decision, not a product-interface decision.
The MCP tools, session lifecycle, bounded output, transcript, process-scoped local API, and human-viewer behavior must remain stable under either backend.

## 2. Product requirements the backend must satisfy

The selected backend must support:

- complete R, Python, and SQL cells in one process;
- native top-level R behavior and truthful stack/source semantics;
- persistent reticulate Python and persistent DuckDB state;
- explicit structured busy, idle, input-required, stopped, and crash states;
- `readline()`, `browser()`, `recover()`, Python `input()`, and debuggers;
- cooperative interrupt plus forced worker termination;
- bounded stdout, stderr, values, tables, help, and diagnostics;
- plots and other display artifacts;
- object inventory and typed sideband inspection;
- retained object references and bounded viewport access for large live tables;
- immutable snapshots that can be explored while the runtime is busy;
- no arbitrary invisible viewer-supplied code;
- process-level sandboxing and honest restart/state-loss boundaries.

The backend may expose additional capabilities, but the supervisor must negotiate them rather than assume them.

## 3. Option A: Ark-backed worker

### 3.1 Shape

```text
MCP and local sidecar clients
          │
          ▼
Rust supervisor
  ├── product session state
  ├── output and transcript policy
  ├── local API and authorization
  └── Ark backend adapter
          │ Jupyter channels + custom comms
          ▼
        Ark
          └── R
                ├── reticulate Python
                └── DuckDB
```

The supervisor acts as the only Jupyter client.
Browser viewers and third-party sidecars use MCP Console's local API, not Ark's sockets or comm schemas directly.

### 3.2 Potential gains

Ark already implements difficult and useful behavior:

- R discovery, startup, event processing, and cross-platform frontend handling;
- complete-cell execution with message identity and busy/idle state;
- stdin and a high-priority control path;
- stdout, conditions, errors, source references, plots, and help;
- debugger and Variables integrations;
- retained references plus a mature Data Explorer backend and custom comm protocol;
- existing frontend behavior used by Positron and potentially reusable by Canvas or another viewer.

The Data Explorer is the strongest reason to consider Ark.
It already models the interaction MCP Console needs: retain a large object in the R session, then fetch only the rows, columns, filters, sorts, profiles, or summaries the human currently needs.

### 3.3 Costs and constraints

Ark is an R Jupyter kernel.
MCP Console still owns or must add:

- first-class R, Python, and SQL submission identity;
- Python and SQL dispatch without misleading R wrappers or source locations;
- compact MCP polling and result formatting;
- named sessions and persistent requirement manifests;
- transcript and output-spool policy;
- sandbox inheritance and process ownership;
- the process-scoped local sidecar service and browser security boundary;
- translation between Ark comms and a stable backend-neutral inspection API.

Operational costs include Jupyter connection files, ZeroMQ channels, comm lifecycle, more processes or threads, a broader dependency graph, version skew, and potentially unused IDE-oriented components.
Ark is valuable only if its reusable behavior outweighs those costs.

The project should not depend on an undocumented internal comm protocol without either a compatibility commitment, pinned tests, or an upstreamed stable boundary.

## 4. Option B: purpose-built native worker

### 4.1 Shape

```text
MCP and local sidecar clients
          │
          ▼
Rust supervisor
  └── native backend adapter
          │ small private protocol
          ▼
Rust worker using mcp-repl / harp / libr
          └── R
                ├── reticulate Python
                └── DuckDB
```

### 4.2 Potential gains

- the internal request model is natively R/Python/SQL rather than R-kernel-centric;
- fewer transports and less translation;
- direct control over output budgets, transcript events, sandboxing, and lifecycle;
- easier to keep the worker minimal and purpose-built;
- no dependency on Ark's Jupyter-facing process shape.

### 4.3 Costs and constraints

`harp` and `libr` are low-level building blocks, not a complete R frontend.
The project becomes responsible for:

- R startup, main-thread ownership, event processing, callbacks, and platform behavior;
- cell parsing, visible-value printing, errors, conditions, source references, and stack fidelity;
- interactive input, debugger behavior, interrupts, shutdown, and crash recovery;
- plots, help, viewer content, object references, and Variables behavior;
- a robust large-data inspection backend, including formatting, filtering, sorting, profiles, staleness, and invalidation.

A simple `df[rows, columns]` helper is not feature parity with Ark's Data Explorer.
The native option should be chosen only if the product can intentionally support a narrower v1 or if the equivalent backend proves tractable.

## 5. Option C: shared lower-level runtime

The preferred long-term architecture is a reusable R-runtime layer below Ark's Jupyter/Positron adapters and below MCP Console's MCP/local-API adapters:

```text
shared R runtime
  ├── startup and event loop
  ├── cell evaluation and stdin
  ├── interrupt and debugger integration
  ├── plots, help, objects, and inspection
  └── normalized events

Ark                 MCP Console
Jupyter/Positron     MCP/local sidecar
```

This reduces duplicated native-R maintenance.
It may require organizational coordination or upstream work and therefore cannot block an initial prototype indefinitely.

## 6. Required spike

Build the smallest end-to-end implementations needed to answer the following with measurements and tests.

### 6.1 Core R behavior

- Can a complete R cell execute with correct visible values and source references?
- What does `sys.calls()` show inside a user function?
- Do errors and tracebacks omit incidental transport details?
- Do `readline()`, `browser()`, `recover()`, menus, and nested prompts work?
- Can the backend pair an input request with a successful-read receipt, so buffered input avoids an extra tool call while partial input still becomes visible promptly?
- Can it keep that receipt distinct from payload or byte-consumption acknowledgment, including for code that reads fd 0 directly?
- Can interrupt recover to idle without silently restarting?

### 6.2 Python and SQL

- Can Python and SQL be first-class evaluation types rather than opaque user-visible R code?
- Are source filenames and diagnostics language-native?
- Do Python `input()` and `pdb` work?
- Can DuckDB use the intended persistent R/DBI connection and live R relations?
- What R frames are truthfully visible when Python or SQL calls back into R?

### 6.3 Human sideband

From an independent process:

1. create a large R data frame;
2. obtain an opaque table reference;
3. request schema and bounded visible rows/columns;
4. sort, filter, and profile without retrieving the full object;
5. scroll repeatedly and measure latency and allocations;
6. mutate or rebind the source and verify revision/invalidation behavior;
7. characterize requests while R is busy, in `browser()`, and waiting for input;
8. create a snapshot and continue browsing it during another evaluation;
9. view plots and help through the same sidecar service.

For Ark, determine exactly which Data Explorer comm APIs and backend components are usable outside Positron.
For the native path, implement enough equivalent typed operations to compare honestly.

### 6.4 Operations

Measure or document:

- startup latency and memory;
- binary and wheel size;
- process and thread count;
- sandbox and local-endpoint requirements;
- behavior on macOS, Linux, and Windows;
- version negotiation and compatibility testing;
- whether changes require an Ark fork;
- expected maintenance ownership.

## 7. Decision matrix

Score each candidate against the following, with evidence rather than preference:

| Criterion | Weight |
| --- | ---: |
| Correct R lifecycle, stdin, debugger, and interrupt behavior | critical |
| Large live-table inspection and plot/help reuse | critical |
| First-class Python and SQL semantics | critical |
| Backend can be isolated behind the normalized service | critical |
| No unmaintainable fork or unstable dependency boundary | critical |
| Packaging, startup, and sandbox complexity | high |
| Cross-platform support | high |
| Output/transcript integration | high |
| Compatibility with existing Positron/Canvas viewers | high |
| Implementation effort and long-term maintenance | high |
| Binary size and incidental dependencies | medium |

## 8. Decision rule

Choose Ark when its mature R and sideband behavior can be reused through a stable adapter, Python/SQL can remain first-class, and the integration does not require a long-lived invasive fork.

Choose the native worker when Ark's R-kernel assumptions, transport, dependency graph, or extension requirements dominate, and a deliberately narrower but sufficient live/snapshot inspection backend can be implemented more cleanly.

Prefer shared extraction when it is feasible without stalling delivery.

After the spike:

1. write `docs/adr/0001-runtime-backend.md`;
2. update `AGENTS.md`, `README.md`, and `ARCHITECTURE.md` to mark the result settled;
3. delete or quarantine the losing prototype;
4. retain backend-neutral integration tests as the product conformance suite.

## 9. Non-negotiable abstraction boundary

Regardless of selection:

- MCP clients see only the two documented MCP tools;
- sidecar clients see only the process-scoped local API;
- viewer traffic is typed inspection, not arbitrary hidden code;
- external arbitrary code is attributed and enters the transcript;
- output remains bounded before entering model context;
- runtime transport and object handles remain private implementation details;
- the local API supports both revisioned live views and immutable snapshots;
- no viewer starts or prolongs a daemon.
