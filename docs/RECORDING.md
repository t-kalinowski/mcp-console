# Recordings and rendering

Recordings live on the **controller**, even when cells run on SSH, Docker, or Docker Sandbox.
They contain source, stdin, requirements, output, and artifacts without redaction.
There is no aggregate quota or automatic cleanup.
Shared paths can expose controller records to the workload; choose them deliberately.

## Location and files

On the first `send`, startup output, or startup failure, Console uses `.agents/console/sessions/<run-id>/` if `.agents/console` already exists in the launch directory.
Otherwise it uses `~/.agents/console/sessions/<run-id>/`, without creating a project `.agents` directory.
`MCP_CONSOLE_HOME` replaces the fallback Console directory, not `HOME`; project selection still takes precedence.

| File                      | Meaning                                                                                         |
| ------------------------- | ----------------------------------------------------------------------------------------------- |
| `internal/events.jsonl`   | Authoritative append-only journal of calls, assembled results, environment and lifecycle events |
| `transcript.md`           | Readable chronological call ledger, with results and artifact links                             |
| `transcript.qmd`          | Source projection for editing and rendering; not a replay                                       |
| `outputs/call-NNNNNN.log` | Raw text for an admitted cell, capped at 1 GiB                                                  |
| `outputs/session.log`     | Startup and idle text outside a cell, capped at 1 GiB                                           |
| `artifacts/`              | Retained image files                                                                            |

Returned raw-log paths are launch-directory-relative for project recordings and absolute for fallback recordings.
Retrieving omitted text requires filesystem access there; Console has no log read/search tool.

The journal uses schema version 2 for startup/session events and nullable metadata before discovery.
In version 2, `artifact_created.call_id` is `null` for session-owned images and an integer for cell-owned images; consumers must select their decoder using `schema_version`.

The journal is flushed before derived projections.
A `tool_result` is recorded before transport delivery, so it does not prove that the client received it.
Polling remains separate calls in the Markdown ledger, not a reconstructed notebook cell with one inferred result.
Calls admitted before discovery completes are retained with their results even if discovery fails.
Recording metadata remains unknown until discovery supplies it; a startup failure does not fabricate runtime capabilities.
Discovery configures the ledger and replays pending records before publishing worker configuration, so startup artifacts follow earlier calls and results.

## Raw output and failures

Cell logs receive console text and direct stdout/stderr before inline preview truncation.
Response cuts flush active logs; reaching a response timeout does not finish the file.
Completion or restart detaches it.
After its cap or a write failure, Console still drains output and counts discarded bytes.

`cell_output` events distinguish retained raw bytes, raw bytes discarded from the file, and rendered UTF-8 bytes omitted from previews.
These counts are not interchangeable: stream normalization can change byte counts, and discarded raw text can still appear in a preview.
The latest summary for a cell owns its cumulative totals.
`session_output` events provide the same counts for startup and idle text; startup images have no call owner.

A journal or artifact failure disables further recording without stopping the worker.
A cell-log failure affects that file and is reported in the response.
Failure to create the session log disables recording and reports the error on server stderr, including sessions that close without a tool call.
Failure of either derived projection disables both projections but leaves the journal and artifacts available.
None of these files is a live-state checkpoint.

## Render a reviewed copy

The QMD contains calls with exactly one R, Python, or SQL source field, including qualifying **rejected calls and failed evaluations**.
It omits stdin, control, results, errors, polls, and recorded artifacts.
Review and edit a copy before executing it outside the worker sandbox.

For a local recording, from the recording directory:

```sh
uv tool run --from r-lib-ir ir render transcript.qmd
```

With `ir` on `PATH`, use `ir render transcript.qmd`.
Rendering requires R on the render host even when the original Console session had no R.
SQL chunks need a user-supplied DBI connection.
Remote/compute recordings identify their target but do not copy its files or reproduce its environment.

Front matter supplies dependency declarations, not a lockfile.
R-present sessions combine built-in defaults and recorded declarations, which need not match every successfully accepted or automatically inferred package.
Managed Python-only sessions also track accepted Python environments and omit rejected candidates; neither mode pins the complete environment or Python version.
Bare sessions omit managed defaults.

Committed `set`/`reset` operations create requirement boundaries and disable QMD evaluation: one header manifest cannot reproduce cells that used incompatible historical environments.
Adjust those boundaries and the rendering environment explicitly rather than treating the document as automatic replay.
