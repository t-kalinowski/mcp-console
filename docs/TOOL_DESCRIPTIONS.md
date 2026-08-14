# Registered MCP Tool Descriptions

**Status:** Implemented v0.3 \
**Date:** 2026-08-13

This document contains the exact descriptions registered with the MCP server.
Keep these synchronized with the implementation; [`MCP_INTERFACE.md`](../design-sketches/docs/MCP_INTERFACE.md) describes a broader intended surface that includes unimplemented fields and actions.
These strings are part of the agent-facing interface and should change only when the added context materially improves tool selection or correct use.
MCP Console selects the built-in or custom-worker profile before initialization and renders one tool surface for the server lifetime.
The built-in profile also reflects whether Python is server-managed or follows inherited `RETICULATE_PYTHON` configuration.
On operating systems without a worker runtime, the tool surface reports that execution and session management are unavailable.

## Built-in worker with managed Python

### `send`

```text
Persistent mixed-language computational workbench. Use it whenever exact computation or direct inspection would improve accuracy—from arithmetic, string counting, parsing, and file or binary-data inspection to data wrangling, exploratory analysis, visualization, statistics, simulation, and model training or tuning. Choose the clearest language for each step and switch freely between calls. The default R environment includes tidyverse, reticulate, DBI, and duckdb, together with their full dependency sets, such as ggplot2, dplyr, readr, and jsonlite. The built-in managed Python environment includes NumPy and pandas. DuckDB SQL is also available. State persists across calls. Python reads R globals through `r.name`; R reads Python globals through `py$name`; SQL queries R data frames by name; R accesses the DuckDB catalog through `sql_connection()`. Language-native help and introspection are available. Do not probe package availability in cells. If you want to use a package, prepare it with `session`, then load it directly with R `library()` or Python `import`. R default-device plots and open `matplotlib.pyplot` figures return as PNG images. Send exactly one complete `r`, `python`, or `sql` cell. Call `send` sequentially; concurrent calls are unsupported. Use `stdin` for interactive reads or debugger commands; omit code and stdin to poll. A wait timeout does not stop computation, and running work must be collected before new code is sent. R errors, Python exceptions, and DuckDB errors are ordinary console output, so inspect result text and continue or correct the cell. Evaluated code can read host files but cannot directly access the network and can write only within the worker's private temporary directory. Managed Python requirement resolution triggered by R code such as `reticulate::py_require()` or by an R package load is a host-side exception: it may access the network and execute installation or build code, so use only trusted requirements.
```

Property descriptions:

- `r`: `` Complete multiline R cell evaluated in persistent global state. The default R environment includes tidyverse, reticulate, DBI, duckdb, and their full dependency sets, such as ggplot2, dplyr, readr, and jsonlite. Packages are not attached automatically. Read Python globals through `py$name`; for example, `df <- tibble::as_tibble(py$df)`. R data frames are directly queryable by name from later SQL cells. Access DuckDB tables and views through the borrowed `sql_connection()` with DBI or dplyr; do not disconnect it. Default-device plots return as PNG images. Keep all drawing operations for one plot in the same cell. Set persistent dimensions with `options(console.plot.width = ..., console.plot.height = ..., console.plot.dpi = ...)`; width and height are in inches. Omit to send stdin or poll. ``
- `python`: `` Complete multiline Python cell evaluated in persistent `__main__` state; its final expression is displayed. The built-in managed Python environment includes NumPy and pandas. Use `session` to prepare other packages such as scikit-learn or Matplotlib. Read R globals and call R functions through `r.name`; for example, `frame = r.df`. Return Python globals to R through `py$name`. Python data frames are not automatically visible to SQL; bind them to an R name first. At cell end, including after a Python error, every open `matplotlib.pyplot` figure returns once as a PNG image and is closed. `show()` is optional. R plots called through `r` follow the R plot rules. Omit to send stdin or poll. ``
- `sql`: `` Complete DuckDB SQL cell evaluated in the persistent catalog. Use it for filtering, joins, aggregation, and tabular inspection. An unqualified relation name can query a data frame in R global state; a DuckDB table or view with the same name takes precedence. Query results return a bounded preview. Use `SHOW TABLES`, `DESCRIBE`, `SUMMARIZE`, and `EXPLAIN` for discovery. DuckDB CLI dot commands are not supported. Omit to send stdin or poll. ``
- `stdin`: `` Text for interactive reads and debugger commands such as R `readline()` or `browser()` and Python `input()`, `breakpoint()`, or `pdb`. Its UTF-8 encoding is queued to worker stdin exactly; no newline is added. Send it with a cell to prequeue input or on its own while the worker is running or idle. If output ends in `[stdin needed]`, send the requested input here. Unread text can satisfy later reads and is discarded by restart. ``
- `timeout_ms`: `` Maximum time this call waits for an evaluation or one automatic worker replacement attempt. On expiry, the call returns available output followed by the current state, such as `[running]` or `[worker starting]`, without stopping the computation or startup. Poll by calling `send` again without `r`, `python`, `sql`, or `stdin`. ``

### `session`

```text
Make additional R or Python packages available, or restart the persistent console session. Do not probe package availability in cells. If you want to use a package, use `prepare`, then load it with R `library()` or Python `import` in `send`. Packages are not imported or attached automatically. An idle server-managed worker can add R and compatible Python requirements without losing live state. After a recoverable live preparation failure, evaluation remains available so state can be saved, but new requirement additions require restart. Requirements are additive, idempotent, and persist across restart. `restart` may optionally add Python requirements, then replaces the worker and loses all in-memory R, Python, and SQL state, debugger state, and unread stdin. Requirement resolution runs outside the execution sandbox and may download packages or execute installation or build code on the host; use only trusted requirements.
```

Property descriptions:

- `action`: `` `prepare` adds R or Python requirements before a server-managed worker starts. After startup, it can add R and compatible Python requirements while the worker is idle. `restart` replaces the worker, optionally adds Python requirements, and starts it if needed. ``
- `requirements`: `` Additive packages to make available. `prepare` requires at least one R or Python entry. `restart` accepts Python entries only; omit `requirements` to restart unchanged. Requirements persist across restart but do not import or attach packages. After a recoverable live preparation failure, evaluation remains available so state can be saved, but new requirement additions return `[restart required]` until restart. The same marker follows a failed automatic replacement. Resolution runs outside the worker sandbox and may download packages or execute installation or build code on the host. Managed Python startup and Matplotlib cache warming also run on the host and may execute selected code; use only trusted requirements. ``
- `requirements.r`: `` Additive, single-line IR package references for `prepare`, for example `data.table`, `sf`, or `yaml12`. An idle server-managed worker can add R requirements without losing live state. Local package sources are rejected because resolution runs with server permissions. ``
- `requirements.python`: `` Additive, single-line PEP 508 requirements for `prepare` or `restart`, for example `polars>=1`, `scikit-learn`, or `matplotlib`. An idle server-managed worker may activate compatible additions without losing state. ``

## Built-in worker with inherited Python configuration

### `send`

```text
Persistent mixed-language computational workbench. Use it whenever exact computation or direct inspection would improve accuracy—from arithmetic, string counting, parsing, and file or binary-data inspection to data wrangling, exploratory analysis, visualization, statistics, simulation, and model training or tuning. Choose the clearest language for each step and switch freely between calls. The default R environment includes tidyverse, reticulate, DBI, and duckdb, together with their full dependency sets, such as ggplot2, dplyr, readr, and jsonlite. Python initially follows inherited `RETICULATE_PYTHON` configuration. A successful `prepare` with Python requirements before the worker starts or `restart` with Python requirements after it starts replaces it with a managed environment. Packages in the active Python environment are not discovered or advertised. DuckDB SQL is also available. State persists across calls. Python reads R globals through `r.name`; R reads Python globals through `py$name`; SQL queries R data frames by name; R accesses the DuckDB catalog through `sql_connection()`. Language-native help and introspection are available. Do not probe package availability in cells. Load R packages directly with `library()`. Import packages provided by the active Python environment directly. If you want to use an additional package, prepare it with `session`. If Python preparation reports `[restart required]`, call `session` with `action = "restart"` and those Python requirements instead; restart loses worker state. R default-device plots and open `matplotlib.pyplot` figures return as PNG images. Send exactly one complete `r`, `python`, or `sql` cell. Call `send` sequentially; concurrent calls are unsupported. Use `stdin` for interactive reads or debugger commands; omit code and stdin to poll. A wait timeout does not stop computation, and running work must be collected before new code is sent. R errors, Python exceptions, and DuckDB errors are ordinary console output, so inspect result text and continue or correct the cell. Evaluated code can read host files but cannot directly access the network and can write only within the worker's private temporary directory. When managed Python is active, requirement resolution triggered by R code such as `reticulate::py_require()` or by an R package load is a host-side exception: it may access the network and execute installation or build code, so use only trusted requirements.
```

The `r`, `sql`, `stdin`, and `timeout_ms` property descriptions are the exact strings registered for managed Python above.
The `python` property changes to:

- `python`: `` Complete multiline Python cell evaluated in persistent `__main__` state; its final expression is displayed. Python initially follows inherited `RETICULATE_PYTHON` configuration. A successful `prepare` with Python requirements before the worker starts or `restart` with Python requirements after it starts replaces it with a managed environment. Packages in the active Python environment are not discovered or advertised. Import packages provided by the active Python environment directly. If you want to use an additional Python package, prepare it with `session`. If Python preparation reports `[restart required]`, call `session` with `action = "restart"` and that Python requirement instead; restart loses worker state. Read R globals and call R functions through `r.name`; for example, `frame = r.df`. Return Python globals to R through `py$name`. Python data frames are not automatically visible to SQL; bind them to an R name first. At cell end, including after a Python error, every open `matplotlib.pyplot` figure returns once as a PNG image and is closed. `show()` is optional. R plots called through `r` follow the R plot rules. Omit to send stdin or poll. ``

### `session`

```text
Make additional R or Python packages available, or restart the persistent console session. Do not probe package availability in cells. Load packages provided by the active Python environment directly with `import`, and load R packages with `library()`. If you want to use an additional package, use `prepare`. If Python preparation reports `[restart required]`, call `restart` with those Python requirements instead; restart loses worker state. Packages are not imported or attached automatically. An idle worker can add R requirements without losing live state. Once Python is managed, it may also activate compatible Python additions without losing live state. After a recoverable live preparation failure, evaluation remains available so state can be saved, but new requirement additions require restart. Requirements are additive, idempotent, and persist across restart. `restart` may optionally add Python requirements, then replaces the worker and loses all in-memory R, Python, and SQL state, debugger state, and unread stdin. Requirement resolution runs outside the execution sandbox and may download packages or execute installation or build code on the host; use only trusted requirements.
```

The `requirements` and `requirements.r` property descriptions are the exact managed-Python strings above.
The other property descriptions change to:

- `action`: `` `prepare` adds R or Python requirements before a worker starts. An idle worker can add R requirements without losing state. Once Python is managed, it may also activate compatible Python additions without losing state. After an inherited Python worker starts, use `restart` with Python requirements; restart loses worker state and starts its replacement. ``
- `requirements.python`: `` Additive, single-line PEP 508 requirements for `prepare` or `restart`, for example `polars>=1`, `scikit-learn`, or `matplotlib`. Before the first worker starts, `prepare` can select managed Python. After an inherited Python worker starts, supply additions to `restart`. Once Python is managed, an idle worker may activate compatible `prepare` additions without losing state. ``

## Custom worker

### `send`

```text
Persistent console backed by the custom worker selected with `serve --worker`. MCP Console passes one complete cell tagged `r`, `python`, or `sql`; the custom worker defines the supported languages and installed packages, together with its state model, interoperability, plotting, and ordinary error behavior. Package availability and loading are worker-defined; package preparation with `session` is unavailable. Call `send` sequentially; concurrent calls are unsupported. Use `stdin` to queue exact UTF-8 text to worker standard input without adding a newline; omit code and stdin to poll. A wait timeout does not stop the worker operation, and running work must be collected before new code is sent. The worker can read host files but cannot directly access the network and can write only within its private temporary directory.
```

Property descriptions:

- `r`: `` Complete multiline cell sent to the custom worker with the `r` language tag. The worker defines how the source is evaluated and returned. Omit to send stdin or poll. ``
- `python`: `` Complete multiline cell sent to the custom worker with the `python` language tag. The worker defines how the source is evaluated and returned. Omit to send stdin or poll. ``
- `sql`: `` Complete multiline cell sent to the custom worker with the `sql` language tag. The worker defines how the source is evaluated and returned. Omit to send stdin or poll. ``
- `stdin`: `` Text queued exactly as UTF-8 bytes to custom worker standard input; no newline is added. Send it with a cell to prequeue input or on its own while the worker is running or idle. If output ends in `[stdin needed]`, send the requested input here. Unread text can satisfy later reads and is discarded by restart. ``
- `timeout_ms`: `` Maximum time this call waits for an evaluation or one automatic worker replacement attempt. On expiry, the call returns available output followed by the current state, such as `[running]` or `[worker starting]`, without stopping the computation or startup. Poll by calling `send` again without `r`, `python`, `sql`, or `stdin`. ``

### `session`

```text
Restart the persistent custom-worker session. Call `session` with `action = "restart"` and omit `requirements`; package preparation and restart-time requirements are unavailable with a custom worker. Restart replaces the worker, starts it if needed, and loses all worker-owned state and unread stdin.
```

The custom-worker schema restricts `action` to `restart`, describes it as `` `restart` replaces the custom worker and starts it if needed. ``, and does not advertise `requirements`.

## Operating system without a worker runtime

### `send`

```text
Console execution and worker stdin are unavailable on this operating system because MCP Console workers are currently supported only on macOS. Calls that submit code or nonempty stdin fail; calls without them can still poll server state.
```

Property descriptions:

- `r`: `` R cells cannot be evaluated because MCP Console workers are currently supported only on macOS. ``
- `python`: `` Python cells cannot be evaluated because MCP Console workers are currently supported only on macOS. ``
- `sql`: `` SQL cells cannot be evaluated because MCP Console workers are currently supported only on macOS. ``
- `stdin`: `` Worker stdin is unavailable because MCP Console workers are currently supported only on macOS. ``

The `timeout_ms` property uses the exact shared description registered for the built-in profile above.

### `session`

```text
Console sessions are unavailable on this operating system because MCP Console workers and managed package environments are currently supported only on macOS. Calls to `session` cannot prepare requirements or restart a worker.
```

Property descriptions:

- `action`: `` `prepare` and `restart` are unavailable because MCP Console workers are currently supported only on macOS. ``
- `requirements`: `` Requirement preparation is unavailable because managed package environments are currently supported only on macOS. ``
- `requirements.r`: `` Managed R libraries are currently supported only on macOS. ``
- `requirements.python`: `` Managed Python environments are currently supported only on macOS. ``

## Inclusion rule

Descriptions should communicate facts that affect whether or how an agent calls the tools: breadth, language selection, persistence, exact interoperability paths, plotting, package preparation, cell/stdin/poll semantics, sandbox boundaries, ordinary language errors, and destructive lifecycle boundaries.

Do not include internal facts that do not change agent behavior: Ark, Jupyter, `harp`, `libr`, worker IPC, stack-frame implementation, the internal JSONL journal, or exact output limits.
Name familiar interfaces such as DuckDB, DBI, dplyr, and the `py`, `r`, and `sql_connection()` bridges when they tell the agent how to complete a workflow.
