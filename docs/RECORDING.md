# Recordings and reports

Console records the agent's calls, text output, and plots on the machine where it runs.
You can use that record to check how it reached a result, inspect output that did not fit in a response, or turn reviewed code into a report.

Recordings contain source, input, requirements, output, and artifacts **without redaction**.
Review them before sharing.

## Read the recording {#location-and-files}

Ask the agent for the recorded session's path, then open `transcript.md` in that directory.
It shows calls and results with links to retained plots and other artifacts.
For the dice example in [Get started](getting-started.qmd), you can inspect both the simulation and the later comparison that reused its results.

The recording documents what happened; it does not save the live Python objects, R objects, or in-memory database for a later session.
To repeat an analysis, you will need its inputs and reviewed code.

## Retrieve omitted output

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

## Turn the code into a report {#render-a-reviewed-copy}

`transcript.qmd` is a starting point for a report, **not a replay or checkpoint**.
It can contain failed evaluations and rejected calls.
It omits interactive input, control operations, output, and recorded images.

Copy it before editing:

```sh
cp transcript.qmd report.qmd
```

Review the code, inputs, package declarations, and evaluation settings in `report.qmd`.
Rendering executes code outside the Console sandbox.

After reviewing the copy:

```sh
uv tool run --from r-lib-ir ir render report.qmd
```

With `ir` already installed, use `ir render report.qmd`.

Rendering requires R, even when the original session used only Python.
SQL chunks need a user-supplied DBI connection.
Package declarations are not a lockfile or a complete history of the environment.

Some recordings disable evaluation because runtime information is unavailable or the session changed between incompatible dependency declarations.
Review those boundaries and choose the rendering environment explicitly rather than simply enabling every chunk.

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

For other workflows, the [documentation index](README.md#task-index) links to guides by what you want to do.
To prepare Console for a particular project or agent, continue with [Customize Console](BUILD_ON_CONSOLE.md).
