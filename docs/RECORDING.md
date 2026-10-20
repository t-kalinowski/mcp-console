# Recordings and reports

Console records the agent's calls, text output, and plots on the machine where it runs.
You can use that record to check how it reached a result, inspect output that did not fit in a response, or turn reviewed code into a report.

Recordings contain source, input, requirements, output, and artifacts **without redaction**.
Review them before sharing.

## Read the recording {#location-and-files}

Ask the agent for the recording's path, then open `transcript.md` in that directory.
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
For SQL, your agent can retrieve the last result in R or Python without running the query again.

Reference: [Retrieve the last SQL result](RUNTIME_REFERENCE.md#retrieve-the-last-sql-result).

## Export a working copy

Ask the agent to export the current session from R:

```r
paths <- .console$export_session("analysis-report")
```

Or from Python, including a session without R:

```python
paths = _console.export_session("analysis-report")
```

Choose a new directory outside the managed recording that the worker is allowed to write.
The helper creates writable `session.qmd`, `session.md`, and any referenced images.
It returns paths named `qmd`, `markdown`, and `images`.

The export includes the current worker session as recorded **before the exporting cell began**.
It excludes that cell's source and unfinished results.
It does not execute code, repeat SQL, recompute plots, or invoke Quarto.

Open `session.md` to read the recorded source and results, including output previews, errors, omission notices, and images.
Results collected by later polls stay with their cells.
Included images use portable relative links; supplementary raw logs are not copied.

`session.qmd` contains source and dependency metadata without captured output.
It can contain failed or rejected submissions and is not a checkpoint or a guarantee of independent reproducibility.
The managed recording also contains source documents in `sessions/session-NNN.qmd`; copy or export them before editing.

See the [recording reference](RECORDING_REFERENCE.md#export-a-working-copy) for the file layout, delivery evidence, and export limitations.

## Render reviewed source {#render-a-reviewed-copy}

Review the exported code, inputs, package declarations, and evaluation settings before executing `session.qmd`.
Rendering executes code outside the Console sandbox.

After reviewing the copy:

```sh
cd analysis-report
uv tool run --from r-lib-ir ir render session.qmd
```

With `ir` already installed, use `ir render session.qmd`.
Rendering requires R, even when the original session used only Python.
SQL chunks need a user-supplied DBI connection.
Package declarations are not a lockfile or a complete history of the environment.

<span id="storage-and-retention"></span> <span id="recording-failures"></span>

Reference: [Recording storage, files, and retention](RECORDING_REFERENCE.md).

For other workflows, the [documentation index](README.md#task-index) links to guides by what you want to do.
To prepare Console for a particular project or agent, continue with [Build a data assistant](BUILD_ON_CONSOLE.md).
