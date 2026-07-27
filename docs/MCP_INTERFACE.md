# MCP Console Interface

**Status:** Draft v0.2  
**Date:** 2026-07-26  
**Scope:** Agent-facing MCP tools and observable behavior

## 1. Interface summary

MCP Console exposes two text-returning tools:

```text
console
console_session
```

`console` handles the frequent path: evaluate one R, Python, or SQL cell; supply interactive stdin; or wait for a running evaluation. `console_session` handles infrequent lifecycle operations.

The interface is optimized for frequent use and global enablement:

- language is encoded by the object key;
- the default session and wait behavior are implicit;
- ordinary success contains only useful console text;
- state markers appear only when needed;
- all responses are bounded;
- larger output and artifacts are ordinary workspace files;
- v1 does not require structured MCP output, resources, or inline images.

## 2. Tool: `console`

### 2.1 Draft schema

```json
{
  "name": "console",
  "description": "Evaluate R, Python, or SQL in a persistent shared session. Send one of r/python/sql for a complete cell, stdin only when input is requested, or no mode field to wait. Sessions are created by their first cell; require adds packages before evaluation.",
  "inputSchema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "r": { "type": "string" },
      "python": { "type": "string" },
      "sql": { "type": "string" },
      "stdin": { "type": "string" },
      "session": {
        "type": "string",
        "default": "default",
        "minLength": 1,
        "maxLength": 64,
        "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$"
      },
      "label": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160
      },
      "require": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "r": {
            "type": "array",
            "items": { "type": "string", "minLength": 1 },
            "maxItems": 64
          },
          "python": {
            "type": "array",
            "items": { "type": "string", "minLength": 1 },
            "maxItems": 64
          }
        }
      },
      "wait_ms": {
        "type": "integer",
        "minimum": 0,
        "maximum": 300000,
        "default": 30000
      }
    }
  }
}
```

The schema intentionally avoids a large `oneOf`. The server performs semantic mode validation and returns a short tool error for invalid combinations.

### 2.2 Modes

| Present mode fields | Operation |
|---|---|
| exactly one of `r`, `python`, `sql` | Evaluate one complete cell |
| `stdin` only | Supply one line to an active input request |
| none of `r`, `python`, `sql`, `stdin` | Wait for or poll the session |
| any other combination | Tool error |

`session`, `label`, `require`, and `wait_ms` are modifiers, not modes.

Additional rules:

- `label` and `require` are accepted only with a code cell.
- A missing session is created only by a code cell.
- Polling, `stdin`, and session-control calls never create a missing session.
- New code is accepted only while the session is idle.
- A session runs one top-level evaluation at a time; code sent while it is busy is rejected rather than queued.
- `stdin` is accepted only while that session has an unsatisfied input request.

### 2.3 Common calls

```json
{"python":"import json\nlogs = json.load(open('logs.json'))"}
```

```json
{"r":"df <- tibble::as_tibble(py$logs)"}
```

```json
{"sql":"select level, count(*) as n from df group by level order by n desc"}
```

```json
{}
```

A non-default session is explicit only when needed:

```json
{"python":"fit_model()","session":"model-fit"}
```

Dependencies are declared with the code that first needs them:

```json
{
  "python":"import polars as pl",
  "require":{"python":["polars>=1"]}
}
```

```json
{
  "r":"library(dplyr)",
  "require":{"r":["dplyr"]}
}
```

A label is optional editorial metadata for the generated transcript:

```json
{
  "sql":"select level, count(*) as n from df group by level",
  "label":"Count records by level"
}
```

It does not affect evaluation or define a runtime identifier.

## 3. Cell semantics

An `r`, `python`, or `sql` value is one complete top-level submission.

- Multiline cells are supported.
- The property name determines the language; source text is never inspected to infer it.
- Incomplete R or Python parser input is an error, not a continuation prompt awaiting another tool call.
- A SQL cell may contain multiple statements if the SQL adapter supports them, but it has one evaluation identity and one final state transition.
- Assignments and state changes performed before a later language error remain unless the language or database transaction semantics roll them back.
- R evaluates top-level expressions in order and auto-prints each visible result, stopping at the first error.
- Python executes statements in persistent `__main__` state and displays the final expression through Python's display hook.
- SQL returns compact statement summaries or a bounded table preview.

The public abstraction is a cell-oriented console, not a notebook document. Submissions are chronological execution messages and are not editable cells owned by the tool.

### 3.1 Execution identity and call stacks

Submitted source remains the source identity for diagnostics, tracebacks, and transcripts. Internal dispatch code must not replace it with a large generated wrapper expression.

R cells are parsed and evaluated at a native top-level boundary. A user call such as:

```r
f <- function() sys.calls()
f()
```

must not contain an MCP Console R closure merely because the cell arrived through MCP. Native top-level error and interrupt contexts may exist internally, but the visible R call stack should begin with user calls.

Python cells use a synthetic filename such as `<mcp-console:python:e0017>` and preserve Python-native traceback locations. The initial implementation may cross a minimal reticulate/R bridge. If Python calls back into R, that real bridge may appear in R introspection; the server does not falsify it.

SQL cells use a synthetic evaluation identity and DuckDB diagnostics. The initial R/DBI implementation may place a small console SQL bridge and DBI/backend calls on the R stack while SQL is active. This is observable only when execution re-enters R or is inspected during that call. Source is stored out of band and bridge calls use a short evaluation ID rather than embedding the complete SQL string.

## 4. Interactive input and debuggers

When evaluated code invokes a supported input consumer—such as R `readline()`, Python `input()`, R `browser()`, `recover()`, or a Python debugger—the session enters `input_required`.

The initiating call returns the prompt and an explicit marker:

```text
Browse[2]>
[input]
```

The next call supplies one logical line:

```json
{"stdin":"where"}
```

The server appends a newline if the value does not already end in one. Send `"\n"` to submit a blank line.

`stdin` is not a new code cell. The active runtime decides whether the line is a debugger command, an expression accepted by the debugger, or ordinary program input. The original evaluation identity remains active until it completes, errors, is interrupted, or the worker stops.

The implementation must distinguish cell source from interactive input structurally. It must not preload a cell as generic console lines that a nested `readline()` or `input()` could accidentally consume.

## 5. Waiting and polling

The initiating code call waits until one of these occurs:

- the evaluation completes or errors;
- the runtime requests input;
- the worker stops;
- `wait_ms` expires.

`wait_ms` limits the MCP wait only. It is not an execution timeout and does not interrupt the runtime.

If the wait expires, the result contains any new bounded output and ends with:

```text
[running]
```

A call with no mode field waits for the selected session:

```json
{}
```

It returns when new output appears, input is requested, the evaluation completes, the worker stops, or its own `wait_ms` expires. `wait_ms: 0` is a nonblocking drain.

An idle session returns:

```text
[idle]
```

A poll against a missing session is a tool error.

MCP request cancellation is distinct from wait expiry:

- cancelling the request that initiated an active evaluation should request interruption of that evaluation;
- cancelling a later poll cancels only that waiter;
- `wait_ms` expiry never cancels the evaluation.

## 6. Dependency declarations

`require.r` and `require.python` are declarative, additive, and session-scoped.

- Python entries are PEP 508 requirement strings.
- R entries use the resolver grammar configured by the implementation.
- Requirements are merged and prepared before the submitted cell begins.
- If preparation fails, the cell does not run.
- Requirements make packages available; they do not import or attach them.
- Repeating an already-satisfied requirement is idempotent.
- Removing packages, downgrading versions, changing interpreter versions, or changing historical cutoffs in a live generation is outside the v1 tool interface. Use a new session or close and recreate the current one.

The resolver may run outside the arbitrary-code worker and populate immutable caches. That implementation detail must preserve the atomic public behavior: prepare, then execute, or fail without execution.

## 7. Tool: `console_session`

### 7.1 Draft schema

```json
{
  "name": "console_session",
  "description": "List, inspect, interrupt, reset, or close console sessions.",
  "inputSchema": {
    "type": "object",
    "additionalProperties": false,
    "required": ["action"],
    "properties": {
      "action": {
        "type": "string",
        "enum": ["list", "status", "interrupt", "reset", "close"]
      },
      "session": {
        "type": "string",
        "default": "default",
        "minLength": 1,
        "maxLength": 64,
        "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$"
      }
    }
  }
}
```

### 7.2 Actions

`list` returns one compact line per existing session:

```text
default    idle
model-fit  running  python  18s
```

`status` returns only facts useful for deciding the next action:

```text
default  generation=1  input_required  r
transcript: .mcp-console/sessions/default/transcript.qmd
```

`interrupt` requests a cooperative interrupt through a control path that is not queued behind the evaluation. It preserves the worker and runtime state when recovery succeeds. It never silently escalates to reset.

`reset` terminates the current worker, increments the session generation, and starts a fresh worker with the same declared requirements and workspace policy. All in-memory R, Python, SQL, debugger, and process state is lost. The transcript records the generation boundary.

`close` terminates the worker and removes the logical session. Retained workspace files and transcripts follow explicit retention policy; closing a session must not silently delete unrelated user files.

## 8. Session state model

Externally meaningful states are:

```text
preparing
starting
idle
running
input_required
stopped
```

A missing session is absent, not another state.

```text
absent
  └─ code cell ─> preparing? ─> starting ─> idle ─> running

running
  ├─ success/error/interrupt ─> idle
  ├─ input request ───────────> input_required ─> running
  └─ crash/exit/kill ─────────> stopped

stopped
  ├─ reset ─> starting
  └─ close ─> absent
```

Visible prompt strings are output. They are never used to infer these states.

## 9. Text result contract

Both tools return MCP text content and advertise no `outputSchema` in v1.

### 9.1 Completed evaluation

Return useful visible output directly:

```text
[1] 2
```

If a completed evaluation has no visible output, return:

```text
[done]
```

### 9.2 Running evaluation

Return the bounded output delta followed by:

```text
[running]
```

### 9.3 Input required

Return the prompt and preceding bounded output followed by:

```text
[input]
```

### 9.4 Stopped worker

```text
[stopped: worker exited with status 1]
```

### 9.5 Language outcomes and tool errors

R errors, Python exceptions, and SQL errors are normal evaluation outcomes. The tool successfully delivered the cell and collected the runtime response, so they normally use `isError: false`.

Use `isError: true` for failures to use or operate the tool, including:

- conflicting mode fields;
- code sent to a busy session;
- `stdin` sent while no input is requested;
- poll or control against an unknown session;
- dependency preparation failure;
- worker startup or private-protocol failure;
- sandbox or authorization failure.

## 10. Oversized output

A single global response budget applies after all text and markers are assembled. The implementation should distinguish three output classes.

### 10.1 Explicit stdout and stderr

All explicit stream output is appended to a per-evaluation file, for example:

```text
.mcp-console/sessions/default/outputs/e0017.log
```

Each MCP reply considers only bytes produced since the previous sealed reply for that evaluation.

If the new delta fits, return it. If it exceeds the budget:

1. return a bounded useful excerpt, normally preserving both the beginning and the most recent tail;
2. state how much was omitted;
3. include the relative path to the complete output;
4. advance the reply cursor to the current end of the spool.

Example:

```text
Loading partition 1...
Loading partition 2...

... 171,204 bytes omitted ...

Loading partition 98...
Loading partition 99...

[truncated: .mcp-console/sessions/default/outputs/e0017.log]
[running]
```

A later poll reports only output created after that reply. It does not force the agent to page through an old truncated backlog. The complete prior output remains available through ordinary host file-reading and search tools.

When an evaluation completes, unread omitted bytes are not injected into later unrelated evaluations. The file path is the continuation mechanism for clients with workspace file tools; v1 does not add a second output-reading protocol.

### 10.2 Large returned values

Known large values should be summarized before full textual materialization:

- tables and relations: bounded rows, columns, cell widths, and bytes;
- arrays: dimensions, type, and a bounded sample;
- mappings and sequences: type, size, and bounded elements;
- SQL results: fetch only enough rows to make the preview.

```text
10,241 rows × 8 columns

 timestamp             level  message
 2026-07-21 12:01:04   INFO   Started
 2026-07-21 12:01:05   ERROR  Connection failed
 ...

[showing 20 rows × 8 columns]
```

The underlying value remains in runtime state for another expression, query, summary, sample, or explicit export.

For an unknown class with an arbitrary user-defined print method, capture its output like any other stream and apply the spool-and-truncate policy. Do not claim a structural preview that the runtime cannot safely produce.

### 10.3 Plots and binary artifacts

Plots and binary outputs are written to the session artifact directory. The text response reports a relative path:

```text
Plot saved: .mcp-console/sessions/default/artifacts/e0021-plot-1.png
```

Viewing the file is delegated to the host agent's ordinary file or image capabilities.

### 10.4 Initial default budgets

Exact values are configurable. Suggested starting defaults:

```text
inline text per reply:  12 KiB
preview rows:           20
preview columns:        12
maximum cell width:     160 characters
```

Tests should assert hard bounds and semantic markers, not incidental table glyphs or terminal widths.

## 11. Transcript contract

Each session exposes:

```text
.mcp-console/sessions/<session>/transcript.qmd
```

The transcript is generated at stable boundaries and contains:

- session and generation metadata;
- stable evaluation IDs;
- optional labels;
- complete submitted source;
- bounded stdout, stderr, result, and error excerpts;
- paths to complete output and artifacts;
- input prompts and supplied interactive lines when safe to record;
- reset, stop, and restart boundaries.

The document is marked non-executing. It is a chronological execution record, not a promise of reproducibility and not a polished notebook. Agents create refined `.qmd`, `.R`, `.py`, or `.ipynb` files separately.

A granular event journal may back transcript recovery. It is internal implementation state, is not advertised as the normal agent artifact, and need not share the QMD format's compatibility guarantees.

## 12. SQL behavior

The SQL frontend uses one persistent DuckDB connection per worker generation. The initial implementation uses the DuckDB R package and DBI; that bridge is not exposed as another agent-facing language.

- DuckDB catalog tables and views persist until reset or worker loss.
- Live R data frames may be visible through DuckDB's R environment scanning.
- A catalog table or view takes precedence over a scanned R object with the same unqualified name.
- Runtime helpers can explicitly register stable R, Python, or Arrow-backed relations under SQL names.
- Python objects may initially cross reticulate/R for conversion or Arrow registration.
- Result-producing statements fetch only enough data to build the bounded preview; they are not automatically collected or spooled in full as text.
- DuckDB CLI dot commands are not SQL and are not supported by the `sql` field. Use SQL-native commands such as `SHOW TABLES`, `DESCRIBE`, and `SUMMARIZE`.
- The DuckDB CLI is a behavioral reference for useful previews and discovery, not an embedded subprocess.

The interface does not promise that every object transfer is zero-copy. Conversions and registration lifetimes must be explicit when they matter.

## 13. Complete interaction example

```json
{"python":"import json\nlogs = json.load(open('logs.json'))"}
```

```text
[done]
```

```json
{
  "r":"df <- tibble::as_tibble(py$logs)\ndplyr::glimpse(df)",
  "require":{"r":["tibble","dplyr"]},
  "label":"Convert logs to a tibble"
}
```

```text
Rows: 10,241
Columns: 8
$ timestamp <chr> ...
$ level     <chr> ...
...
```

```json
{
  "sql":"select level, count(*) as n from df group by level order by n desc",
  "label":"Count records by level"
}
```

```text
level     n
INFO   8231
WARN   1450
ERROR   560
```

```json
{"r":"browser()"}
```

```text
Called from: top level
Browse[1]>
[input]
```

```json
{"stdin":"where"}
```

```text
where 1: browser()
Browse[1]>
[input]
```

```json
{"stdin":"c"}
```

```text
[done]
```

## 14. Deliberately omitted from v1

- MCP resources as the primary oversized-output mechanism;
- a generic `read_output` operation when host file tools can read session files;
- structured output and `outputSchema`;
- inline MCP images;
- a notebook-export MCP tool;
- prose-only transcript cells;
- per-submission include/omit flags for a future curated artifact;
- automatic variable or package inventories;
- line-by-line parser continuation for top-level code;
- DuckDB CLI dot commands;
- automatic scanning of every Python global as a SQL relation;
- silent worker restart;
- Jupyter-kernel or Ark-specific operations;
- MCP task augmentation.

Reconsider an omitted feature only when a concrete client or workflow cannot be served by the two-tool, text-and-files model.
