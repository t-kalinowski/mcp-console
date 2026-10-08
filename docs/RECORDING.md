# Recordings and rendering

Recordings live on the host running Console.
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
When a preview's location exceeds 512 UTF-8 bytes, it instead gives the exact `sessions/<run-id>/...` path, labelled relative to the Console recording directory selected above.
Retrieving omitted text requires filesystem access there; Console has no log read/search tool.

The journal schema is unversioned, with nullable runtime metadata before discovery.
`artifact_created.call_id` is `null` for session-owned images and an integer for cell-owned images.

The journal is flushed before derived projections.
A `tool_result` is recorded before transport delivery, so it does not prove that the client received it.
Polling remains separate calls in the Markdown ledger, not a reconstructed notebook cell with one inferred result.
Calls admitted before discovery completes are retained with their results even if discovery fails.
The session timestamp is captured before those calls, even when the files are created later.
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
Native R startup runs before Console's plot device attaches; its plots use R's native device rather than recorded Console images.
Failed discovery and explicit retries share the same session log, preserving earlier bytes and appending later preparation, startup and idle output until connection closure.

A journal or artifact failure disables further recording without stopping the worker.
A cell-log failure affects that file and is reported in the response.
Failure to create the session log disables recording and reports the error on server stderr, including sessions that close without a tool call.
Failure of either derived projection disables both projections but leaves the journal and artifacts available.
None of these files is a live-state checkpoint.

## Render a reviewed copy

The QMD contains calls with exactly one R, Python, or SQL source field, including qualifying **rejected calls and failed evaluations**.
It omits stdin, control, results, errors, polls, and recorded artifacts.
Review and edit a copy before executing it outside the worker sandbox.
Until discovery supplies runtime metadata, the QMD marks its environment as unknown and disables evaluation without inventing a dependency manifest.
Failed discovery preserves that state and the submitted source.

For a local recording, from the recording directory:

```sh
uv tool run --from r-lib-ir ir render transcript.qmd
```

With `ir` on `PATH`, use `ir render transcript.qmd`.
Rendering requires R on the render host even when the original Console session had no R.
SQL chunks need a user-supplied DBI connection.

Front matter supplies dependency declarations, not a lockfile.
Sessions use the captured configured startup declaration and recorded additions, which need not match every successfully accepted or automatically inferred package.
Managed Python sessions also track accepted Python environments and omit rejected candidates; neither mode pins the complete environment or Python version.
Bare sessions omit managed defaults.

Committed `set`/`reset` operations create requirement boundaries and disable QMD evaluation: one header manifest cannot reproduce cells that used incompatible historical environments.
Adjust those boundaries and the rendering environment explicitly rather than treating the document as automatic replay.
