# Registered MCP Tool Descriptions

**Status:** Implemented v0.3 \
**Date:** 2026-08-13

This document contains the exact descriptions registered with the MCP server.
Keep these synchronized with the implementation; [`MCP_INTERFACE.md`](../design-sketches/docs/MCP_INTERFACE.md) describes a broader intended surface that includes unimplemented fields and actions.
These strings are part of the agent-facing interface and should change only when the added context materially improves tool selection or correct use.

## `send`

```text
Persistent mixed-language computational workbench. Use it whenever exact computation or direct inspection would improve accuracy—from arithmetic, string counting, parsing, and file or binary-data inspection to data wrangling, exploratory analysis, visualization, statistics, simulation, and model training or tuning. Choose the clearest language for each step and switch freely between calls: base R or prepared packages such as dplyr and ggplot2; Python packages such as pandas, NumPy, scikit-learn, and Matplotlib; or DuckDB SQL. State persists across calls. Python reads R globals through `r.name`; R reads Python globals through `py$name`; SQL queries R data frames by name; R accesses the DuckDB catalog through `sql_connection()`. Language-native help and introspection are available. Use `session` to prepare missing packages before loading or importing them. R default-device plots and open `matplotlib.pyplot` figures return as PNG images. Send exactly one complete `r`, `python`, or `sql` cell. Call `send` sequentially; concurrent calls are unsupported. Use `stdin` for interactive reads or debugger commands; omit code and stdin to poll. A wait timeout does not stop computation, and running work must be collected before new code is sent. R errors, Python exceptions, and DuckDB errors are ordinary console output, so inspect result text and continue or correct the cell. Evaluated code can read host files but cannot directly access the network and can write only within the worker's private temporary directory. Managed Python requirement resolution triggered by R code such as `reticulate::py_require()` or by an R package load is a host-side exception: it may access the network and execute installation or build code, so use only trusted requirements.
```

Property descriptions:

- `r`: `` Complete multiline R cell evaluated in persistent global state. Use base R for statistics and modeling, or prepared packages such as dplyr and ggplot2. Read Python globals through `py$name`; for example, `df <- tibble::as_tibble(py$df)`. R data frames are directly queryable by name from later SQL cells. Access DuckDB tables and views through the borrowed `sql_connection()` with DBI or dplyr; do not disconnect it. Default-device plots return as PNG images. Keep all drawing operations for one plot in the same cell. Set persistent dimensions with `options(console.plot.width = ..., console.plot.height = ..., console.plot.dpi = ...)`; width and height are in inches. Omit to send stdin or poll. ``
- `python`: `` Complete multiline Python cell evaluated in persistent `__main__` state; its final expression is displayed. Use prepared packages such as pandas, NumPy, scikit-learn, and Matplotlib. Read R globals and call R functions through `r.name`; for example, `frame = r.df`. Return Python globals to R through `py$name`. Python data frames are not automatically visible to SQL; bind them to an R name first. At cell end, including after a Python error, every open `matplotlib.pyplot` figure returns once as a PNG image and is closed. `show()` is optional. R plots called through `r` follow the R plot rules. Omit to send stdin or poll. ``
- `sql`: `` Complete DuckDB SQL cell evaluated in the persistent catalog. Use it for filtering, joins, aggregation, and tabular inspection. An unqualified relation name can query a data frame in R global state; a DuckDB table or view with the same name takes precedence. Query results return a bounded preview. Use `SHOW TABLES`, `DESCRIBE`, `SUMMARIZE`, and `EXPLAIN` for discovery. DuckDB CLI dot commands are not supported. Omit to send stdin or poll. ``
- `stdin`: `` Text for interactive reads and debugger commands such as R `readline()` or `browser()` and Python `input()`, `breakpoint()`, or `pdb`. Its UTF-8 encoding is queued to worker stdin exactly; no newline is added. Send it with a cell to prequeue input or on its own while the worker is running or idle. If output ends in `[stdin needed]`, send the requested input here. Unread text can satisfy later reads and is discarded by restart. ``
- `timeout_ms`: `` Maximum time this call waits for an evaluation. On expiry, the call returns available output followed by `[running]` without stopping the computation. Poll by calling `send` again without `r`, `python`, `sql`, or `stdin`. ``

## `session`

```text
Make R or Python packages available, or restart the persistent console session. Use `prepare` before loading or importing missing packages. Packages are not imported or attached automatically. Prepare anticipated R packages before the worker starts. After startup, a `prepare` call containing any new R requirement returns `[restart required]` and applies none of that call's R or Python additions; start a fresh server to add R packages. An idle server-managed worker can add compatible Python requirements without losing live state. Requirements are additive, idempotent, and persist across restart. `restart` may optionally add Python requirements, then replaces the worker and loses all in-memory R, Python, and SQL state, debugger state, and unread stdin. Requirement resolution runs outside the execution sandbox and may download packages or execute installation or build code on the host; use only trusted requirements.
```

Property descriptions:

- `action`: `` `prepare` adds R or Python requirements before a server-managed worker starts. After startup, it can add compatible Python requirements while the worker is idle; a new R requirement instead returns `[restart required]` and applies none of that call's additions. `restart` replaces the worker, optionally adds Python requirements, and starts it if needed. ``
- `requirements`: `` Additive packages to make available. `prepare` requires at least one R or Python entry. `restart` accepts Python entries only; omit `requirements` to restart unchanged. Requirements persist across restart but do not import or attach packages. Resolution runs outside the worker sandbox and may download packages or execute installation or build code on the host; use only trusted requirements. ``
- `requirements.r`: `` Additive, single-line IR package references for `prepare`, for example `dplyr`, `ggplot2`, or `jsonlite`. Prepare new R requirements before the worker starts. After startup, a `prepare` call containing any new R requirement returns `[restart required]` and applies none of that call's R or Python additions; start a fresh server to add R packages. Local package sources are rejected because resolution runs with server permissions. ``
- `requirements.python`: `` Additive, single-line PEP 508 requirements for `prepare` or `restart`, for example `pandas>=2`, `scikit-learn`, or `matplotlib`. An idle server-managed worker may activate compatible additions without losing state. ``

## Inclusion rule

Descriptions should communicate facts that affect whether or how an agent calls the tools: breadth, language selection, persistence, exact interoperability paths, plotting, package preparation, cell/stdin/poll semantics, sandbox boundaries, ordinary language errors, and destructive lifecycle boundaries.

Do not include internal facts that do not change agent behavior: Ark, Jupyter, `harp`, `libr`, worker IPC, stack-frame implementation, the internal JSONL journal, or exact output limits.
Name familiar interfaces such as DuckDB, DBI, dplyr, and the `py`, `r`, and `sql_connection()` bridges when they tell the agent how to complete a workflow.
