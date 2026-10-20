# Recording reference

Console retains calls, source, input, requirements, output, and artifacts on the execution host, without redaction.
For reading a recording, retrieving omitted output, or making a report, see [Recordings and reports](RECORDING.md).

## Storage and retention

If `.agents/console` already exists in the launch directory, recordings use `.agents/console/sessions/<run-id>/`.
Otherwise they use `~/.agents/console/sessions/<run-id>/`; `MCP_CONSOLE_HOME` replaces that fallback Console directory.
Console does not create a project `.agents` directory just to select project storage.
There is no aggregate quota or automatic cleanup, so remove old sessions deliberately.

Recording starts when a call, startup output, or startup failure needs it.
Each cell gets a log file when execution starts, even if it produces no text output.

| File                      | Use                                                |
| ------------------------- | -------------------------------------------------- |
| `transcript.md`           | Read calls, results, and links to artifacts.       |
| `transcript.qmd`          | Review submitted source and edit it into a report. |
| `internal/events.jsonl`   | Inspect session events as JSON.                    |
| `outputs/call-NNNNNN.log` | Read raw text from a code cell, up to 1 GiB.       |
| `outputs/session.log`     | Read startup and idle text, up to 1 GiB.           |
| `artifacts/`              | Find retained images.                              |

`internal/events.jsonl` records one JSON object per event, including tool calls and results, dependency selections, and references to output logs and images.
It can be useful for inspecting a session with scripts or investigating a failed call.
Its format may change between Console releases.

Returned paths refer to the Console host.
Project paths are normally relative to the launch directory; fallback paths are absolute.
A long-location notice may instead name a path relative to the selected Console directory.

<span id="recording-failures"></span>

Recording can fail while computation continues.
A notice identifies files or artifacts that are unavailable or only partially retained; do not assume all output was saved.

The recording journal and generated file formats are not independently versioned APIs.
Implementation and failure-handling details belong in [Architecture](ARCHITECTURE.md#output-and-delivery).
