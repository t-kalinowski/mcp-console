# Recording reference

For reading a recording, retrieving omitted output, or exporting a report, see [Recordings and reports](RECORDING.md).

A recording belongs to one Console process, from startup to shutdown, and can contain several worker sessions.
Its capture retains protocol events, submitted source, input, requirements, emitted text, and images **without redaction**.
Review a recording before sharing it.
There is no aggregate quota or automatic cleanup.

## Storage and retention

If `.agents/console` already exists in the launch directory, recordings use `.agents/console/sessions/<run-id>/`.
Otherwise they use `~/.agents/console/sessions/<run-id>/`; `MCP_CONSOLE_HOME` replaces that fallback Console directory.
Console does not create a project `.agents` directory to select project storage.
Existing recordings are not migrated or rewritten.

The first MCP `tools/call` request targeting `send` activates recording, including polls, controls, requirement requests, and rejected submissions.
Initialization, discovery, pings, startup output, and startup failures alone do not create a persistent recording directory.
Console temporarily retains the preceding capture and publishes it in its original order when `send` arrives.
A process that exits without `send` discards that temporary capture.
The recorded process start time remains separate from recording activation time.

```text
<recording>/
├── capture/
│   ├── events.jsonl
│   └── sessions/
│       ├── 001/
│       │   ├── startup.txt
│       │   ├── startup-001.png
│       │   ├── cell-001.txt
│       │   ├── cell-001-001.png
│       │   ├── cell-001-post.txt
│       │   └── cell-002.txt
│       └── 002/
│           └── cell-001.txt
├── transcript.md
└── sessions/
    ├── session-001.qmd
    └── session-002.qmd
```

`capture/events.jsonl` is the authoritative event record.
It retains MCP requests, responses, notifications, environment information, worker transitions, source associations, output ranges, images, and assembled results.
Use it to inspect a recording with scripts or investigate a failed call.
Routine protocol exchanges appear there without filling the readable transcript.
Sequence numbers and timestamps establish observation order; filenames do not establish a timeline across independent streams.
Preparation output observed before a worker exists is retained in process output events rather than assigned to a fabricated session.

`transcript.md` covers the whole recording: calls, source, results, polls, input, controls, failures, session boundaries, and links to retained files.
Each session QMD contains its submitted source and relevant dependency metadata, including failed and rejected source associated with that worker.
It contains no captured results, images, polls, or controls.
A session without source need not have a QMD.
Both document types can be reconstructed from capture without executing recorded code.
They are not checkpoints or guarantees of independent reproducibility.

A new worker lifetime starts a new session, including automatic replacement.
A restart request by itself does not establish a session, and interrupting a surviving worker keeps the same session.
Cell numbers restart at one in each session.

## Output intervals and omitted output

Text observed before a session's first cell starts belongs to `startup.txt`.
Text observed while a cell executes belongs to `cell-NNN.txt`.
Text observed after completion and before the next cell starts belongs to `cell-NNN-post.txt`, including final shutdown output.
If no cell starts, output stays in the startup interval.
These intervals describe when Console observed output, not which computation caused it.
Collecting output in a later poll does not change its ownership.

Every executed cell gets a text file, even if empty.
Startup and post-cell text files are created only when there is text to retain.
Each retained image gets a separate numbered file in its interval.
The existing image admission limits still apply; image omission events and response notices identify emissions that were not retained.
Text and images can alternate; event text ranges preserve that interleaving while each interval keeps one text file.
Console does not collect arbitrary files written by user code.

Raw text retention is limited to 1 GiB per interval, independently of response preview limits.
Console limits how much text it sends to the agent at once.
When a response leaves out text, a notice points to the existing log file and says if any output could not be saved.
The agent can read that file with its client's file-reading tools, without running the code again.

The agent can also read the log while the code is still running.
Console updates the log before sending each response.
Reading it does not remove output from later responses.

Logs contain the text that R, Python, or SQL displayed.
For example, if a table's display includes only a few rows, the log contains that abbreviated display.
To get the complete data, ask the agent to fetch it or save it to a file.
For SQL, your agent can [retrieve the last result](RUNTIME_REFERENCE.md#retrieve-the-last-sql-result) in R or Python without running the query again.

Returned recording paths refer to the Console host.
Project paths are normally relative to the launch directory; fallback paths are absolute.
A long-location notice may use a path relative to the selected Console directory.
Raw retention diagnostics name their interval file relative to the recording, so the location remains valid when temporary startup capture is published.

## Export a working copy

From R:

```r
paths <- .console$export_session("analysis-report")
```

From Python, including a session without R:

```python
paths = _console.export_session("analysis-report")
```

The destination must not already exist and must be outside the managed recording.
Console creates missing parent directories when the worker's filesystem permissions allow it.
The helper returns `qmd`, `markdown`, and `images` paths: an R list of character paths or a Python dictionary containing `pathlib.Path` objects.
R export does not initialize Python.

```text
analysis-report/
├── session.qmd
├── session.md
└── images/          # When the recorded results contain images
```

The export includes the current worker session as recorded **before the cell invoking `export_session` began**.
That cell's source and unfinished results are excluded.
A later export takes a new snapshot; it does not update an earlier destination.

`session.qmd` contains source and dependency metadata without captured output.
Review the code and provide its required data, connections, and environment before executing it.
The original working directory is descriptive metadata, not an execution root imposed on the copy.
The document includes an `ir render session.qmd` hint; IR rendering requires R, even when the recorded session used only Python.
Each chunk permits errors on execution so failed submissions can remain in the document without stopping later chunks.
These options do not assert that a submission succeeded or failed in the recorded session.

`session.md` assembles recorded source and client-facing results into a report with output blocks, errors, and images.
Results collected by later polls remain associated with their actual cells.
Startup and between-cell output remain distinguishable, and polling/control request wrappers are omitted.
Output previews and omission notices are preserved; the export does not expand raw logs into results the model was never shown.
Raw logs mentioned in those notices are supplementary and are not copied.
Included images keep their encoded format and use portable relative links.

Export does not run Quarto, execute source, repeat SQL, or recompute plots.
The worker writes the files under the same filesystem and sandbox permissions as submitted code.
Destination collisions, permission denial, and unavailable capture produce runtime errors.
Export requires a UTF-8 recording path; an unsupported path produces a runtime error without retiring the worker.
Exported files are writable and do not depend on the original recording for their included content or images.

A recorded result proves response assembly.
Console also records whether the existing transport write completed; neither proves that a model read the result.
An export labels responses without confirmed transport writes.

<span id="recording-failures"></span>

## Managed files and failures

The recording directory is a Console-managed record, not a working directory.
Inspect it or export/copy material before editing.
Completed files become read-only while preserving their privacy permissions.
Cell logs and images can finish before the process; startup and post-cell logs stay writable until their boundaries, session QMDs until session updates finish, and events/transcript until process finalization.

Read-only permissions discourage accidental edits; they are not a security boundary.
Abrupt termination can leave files writable.
Recording and projection failures do not stop computation.
Diagnostics identify unavailable or partial capture, and a projection failure can leave capture usable.
If a stalled journal writer exhausts the bounded protocol backlog, Console disables recording and reports the failure on stderr while continuing to receive MCP traffic.
An explicit failed export is an error, not a successful partial report.

The capture and generated document formats are internal formats without independent versioning.
Implementation ownership is described in [Architecture](ARCHITECTURE.md#recording-ownership-and-failures).
