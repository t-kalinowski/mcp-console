# Recordings and reports

Recordings live on the Console host and contain source, stdin, requirements, output, and artifacts **without redaction**.
There is no aggregate quota or automatic cleanup.
Review files before sharing them and remove old sessions deliberately.

## Location and files

If `.agents/console` already exists in the launch directory, recordings use `.agents/console/sessions/<run-id>/`.
Otherwise they use `~/.agents/console/sessions/<run-id>/`; `MCP_CONSOLE_HOME` replaces that fallback Console directory.
Console does not create a project `.agents` directory merely to select project storage.

Recording begins when the first call, startup output, or startup failure needs it.

| File                      | Purpose                                                    |
| ------------------------- | ---------------------------------------------------------- |
| `transcript.md`           | Chronological call ledger with results and artifact links. |
| `transcript.qmd`          | Source projection to review and edit into a report.        |
| `outputs/call-NNNNNN.log` | Raw text for an admitted cell, up to 1 GiB.                |
| `outputs/session.log`     | Startup and idle text, up to 1 GiB.                        |
| `artifacts/`              | Retained images.                                           |
| `internal/events.jsonl`   | Internal append-only journal underlying the projections.   |

Returned paths identify files on the Console host.
Project paths are normally launch-relative; fallback paths are absolute.
Long-location notices can instead name an exact path relative to the selected Console directory.

## Retrieve omitted output

Use the client's filesystem tools to read the named raw log.
Console has no file-read/search tool.
Reading a file does not move the polling cursor, and resubmitting a cell is not a way to retrieve its output.

Raw logs receive output before preview truncation and are flushed at response cuts, so they can be read while a cell runs.
Limits, write failures, or early image rejection can leave only partial retained output; omission notices distinguish that from a complete recording.

No log can recover values that a language printer or SQL preview never emitted.
Fetch or save the complete data explicitly when it matters.

## Render a reviewed copy

`transcript.qmd` is **not a replay or checkpoint**.
It contains qualifying submitted source, including failed evaluations and rejected calls.
It omits interactive input, control operations, output, and recorded images.
Review a copy before executing it; rendering runs outside the worker sandbox.

From the recording directory:

```sh
uv tool run --from r-lib-ir ir render transcript.qmd
```

With `ir` already on PATH, use `ir render transcript.qmd`.
Rendering requires R even for a Python-only original session.
SQL chunks need a user-supplied DBI connection.

Front matter supplies declarations, not a lockfile or a complete installed-environment inventory.
It may not reproduce every automatic addition or historical environment.
Committed `set`/`reset` boundaries disable evaluation because one header cannot represent incompatible environments.
Review those boundaries and choose the rendering environment explicitly.

When runtime discovery failed or has not supplied metadata, the QMD keeps the environment unknown and evaluation disabled rather than inventing requirements.

## Recording failures

A journal or artifact failure disables further recording without stopping computation.
A raw-log failure affects that file.
Failure of a derived projection disables the projections but can leave the journal and artifacts available.
Diagnostics and notices report unavailable or partial retention.

Journal events and generated projections are internal formats, not independently versioned public APIs.
A journaled result records response assembly, not confirmed client receipt.
See [Architecture](ARCHITECTURE.md#output-and-delivery) for ownership and [Open work](TODO.md) for retention and export gaps.
