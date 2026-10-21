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

`transcript.md` covers the whole recording: calls, source, results, polls, input, controls, failures, session boundaries, and links to retained files.
Each session QMD contains its submitted source and relevant dependency metadata, including failed and rejected source associated with that worker.
Dependency metadata includes accepted requirements; rejected additions do not change it.
It contains no captured results, images, polls, or controls.
A session without source need not have a QMD.
Both document types can be reconstructed from capture without executing recorded code.
They are not checkpoints or guarantees of independent reproducibility.

### Output intervals

Each actual worker session numbers executed cells from `001`.
Polls, input, controls, and rejected source do not create cells.
Source remains in the journal and documents even when it does not execute.

Output belongs to the interval in which Console observes it:

- `startup`: before the first cell begins, including the entire lifetime of a worker that executes no cells.
- `cell-NNN`: while that cell executes.
- `cell-NNN-post`: after the cell ends and before the next begins.

The final interval includes shutdown output.
A later poll can return output from several intervals or sessions without changing its ownership.
Console does not infer which computation caused background output or impose chronology across independent streams.

Every executed cell has a text file, including silent cells.
Startup and post-cell text files are created only when text arrives.
One file retains each interval's raw text, up to 1 GiB, even when text and images alternate.
Each retained image has its own numbered file in that interval; PNGs use `.png`, and other emitted image formats use `.bin`.
Raw images have a separate budget per startup, cell, or post-cell interval: 64 MiB of decoded image data, 4,096 files, and 64 KiB of MIME metadata.
An image is saved whole only if it fits the remaining raw budget.
The response preview has its own limits: images omitted from a preview can still be saved, and images that exceed the raw budget can still appear in a response.
Console retains emitted text and images, not arbitrary files written by submitted code.
Output observed before worker acquisition or after worker termination is retained in process-output events rather than assigned to a worker session.
Omission notices for that output reference the capture journal.

### Capture events and completion

`capture/events.jsonl` records one JSON object per event, including MCP requests, responses, and notifications, canonical tool calls/results, worker lifetimes, dependency selections, and references to output logs and images.
Protocol events reference the canonical send call/result records instead of duplicating their payloads.
Events preserve observation timestamps and sequence numbers and identify the session, cell, and interval explicitly.
Text events give byte ranges in the interval log so text/image ordering can be reconstructed.
Each retained image has an `image_retained` event with its owner, artifact ID, and file path relative to the recording directory.
An `image_omitted` event records an image rejected by the raw retention budget; it has no file to locate.
For an image returned without raw retention, the result record has `retained: false` and no file reference; it does not duplicate the image data in the journal.
Result records associate fragments of the assembled response with their observed intervals, preserving previews, prompts, errors, omission notices, and image references.
A response assembly event and a completed local transport write are separate observations.
Neither proves that the client acknowledged the response or that a model read it.

Worker events record actual start, readiness, and termination.
The `cell_started` event associates a call with its executed cell; accepted source that is withheld before execution remains in `tool_call` without a cell event or log.
A restart request does not itself establish a session; a failed preparation or launch that never acquires a worker has no session number.
Interrupting a surviving worker stays in its session.
The journal can help inspect a recording with scripts or investigate a failed call.
Its format may change between Console releases.

The recording directory is a Console-managed record, not a working directory.
Inspect it or export/copy material before editing.
Completed managed files become read-only while retaining their existing privacy permissions.
Cell logs close at their cell boundary; startup and post-cell logs stay writable until their next boundary.
Images become read-only after writing.
Session QMDs become read-only after session-related updates finish.
The journal and recording-wide transcript remain writable until worker output, queued capture, result writers, and transport settlement can no longer update them.
Directories remain writable while Console needs them.
These permissions protect against accidental edits, not unauthorized access; abrupt termination can leave files writable.

Returned paths refer to the Console host.
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
Export requires UTF-8 recording and resolved destination paths; an unsupported destination is rejected before creating directories or files.
An unsupported path produces a runtime error without retiring the worker.
Exported files are writable and do not depend on the original recording for their included content or images.

A recorded result proves response assembly.
Console also records whether the existing transport write completed; neither proves that a model read the result.
An export labels responses without confirmed transport writes.

<span id="recording-failures"></span>

## Recording failures

Recording and projection failures do not stop computation.
Diagnostics identify unavailable or partial capture, and a projection failure can leave capture usable.
If a stalled journal writer exhausts the bounded protocol backlog, Console disables recording and reports the failure on stderr while continuing to receive MCP traffic.
An explicit failed export is an error, not a successful partial report.

The capture and generated document formats are internal formats without independent versioning.
Implementation ownership is described in [Architecture](ARCHITECTURE.md#recording-ownership-and-failures).
