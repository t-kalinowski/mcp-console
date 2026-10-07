//! Named prose sections; selection belongs to the captured presentation profile.

pub(super) const BUILTIN_SCOPE: &str =
    "Persistent R, Python, and SQL workbench for calculations, data analysis, and plots.";

pub(super) const INTERFACE: &str =
    "Language fields describe the configured interface, not installed runtimes. ";

pub(super) const MANAGED_PREPARATION: &str = "Managed resolution needs execution-host resolver support. Bare runtimes and explicitly selected Python use preinstalled packages.";

pub(super) const MANAGED_PREPARATION_SELECTED: &str = "Managed resolution needs execution-host resolver support; bare runtimes use preinstalled packages.";

pub(super) const SEND_WORKFLOW: &str =
    " cell per call. Inspect its output and reuse persistent objects.";

pub(super) const DISPLAY: &str =
    " Results display automatically; supported plots return as images.";

pub(super) const SEND_ORDERING: &str = "Code-bearing calls must be sequential; a control-only interrupt may overlap a pending `send`. Cells are not transactional; effects before an error may remain.";

pub(super) const POLLING: &str =
    "Poll running work with an empty `send`; do not resubmit the cell.";

pub(super) const OUTPUT: &str = "Text results, including notices, have an 8 KiB limit, retaining the beginning and latest tail; images have separate limits. Response notices report state and omitted-output locations.";

pub(super) const CUSTOM_SCOPE: &str = "Persistent custom-worker workbench.";

pub(super) const CUSTOM_CAPABILITIES: &str = "Language fields describe the configured interface; evaluation, display, SQL, and sharing depend on the worker. Console does not supply built-in runtime packages, import hooks, or a default SQL connection. Managed requirements require host resolver support and compatible worker callbacks; Python requirements are unavailable.";

pub(super) const CUSTOM_SELECTED_CAPABILITIES: &str = "Language fields describe the configured interface; evaluation, display, connections, and sharing depend on the worker. Console does not supply built-in runtime packages, import hooks, or a default database connection. Managed requirements require host resolver support and compatible worker callbacks; registry package preparation is unavailable.";

pub(super) const CUSTOM_SWITCHING: &str =
    " Switch languages when useful, using the worker's capabilities.";

pub(super) const WINDOWS_SCOPE: &str =
    "Persistent R and Python workbench for local execution on Windows. SQL is not yet supported.";

pub(super) const SQL_SELECTION: &str = "For databases and structured files, consider DuckDB SQL first for inspection, joins, and aggregation. ";

pub(super) const R_SELECTION: &str =
    "Use R for vectorized data and string operations, statistics, and plots. ";

pub(super) const PYTHON_SELECTION: &str =
    "Use Python when its libraries or format-specific parsing simplify the task. ";

pub(super) const SWITCHING: &str = "Switch languages when useful.";

pub(super) const SQL_FILES: &str =
    "DuckDB can query CSV, Parquet, JSON, and JSONL directly, including nested JSON. ";

pub(super) const SQL_DEFAULTS: &str = "Built-in managed defaults include SQLite when preparation is available; otherwise extensions must be preinstalled. ";

pub(super) const CUSTOM_R: &str = "One complete R cell, if supported by the custom worker.";

pub(super) const CUSTOM_PYTHON: &str =
    "One complete Python cell, if supported by the custom worker.";

pub(super) const CUSTOM_SQL: &str =
    "One complete SQL cell; the custom worker supplies the connection, dialect, and display.";

pub(super) const R_RUNTIME: &str = r#"One complete R cell in persistent global state. Visible top-level expressions autoprint; leave the primary result last. With managed resolution, use missing CRAN packages directly through `library()`, `require()`, `requireNamespace()`, `loadNamespace()`, `::`, or `:::`; avoid availability probes or `install.packages()`."#;

pub(super) const R_BRIDGE: &str =
    " With both runtimes and their bridge available, access Python globals through `py$name`.";

pub(super) const R_SQL: &str = r#" `sql_connection()` returns the R-owned SQL connection for DBI/dplyr. Select a user-owned DBI connection for SQL cells with `console_sql_connection(connection)`; restore managed DuckDB with `console_sql_connection(NULL)`. Never disconnect the managed connection; restore it before disconnecting a selection."#;

pub(super) const R_PLOTS: &str = r#" Default-device plots return as PNGs and finalize at cell end, including after errors; draw each plot in one cell. Explicit devices are not captured. Set persistent dimensions in inches and DPI with `options(console.plot.width_in = ..., console.plot.height_in = ..., console.plot.dpi = ...)`."#;

pub(super) const PYTHON_RUNTIME: &str = r#"One complete Python cell in persistent `__main__` state. The final visible expression autoprints; leave the primary result last. Managed imports can resolve missing PyPI distributions; use `requirements.python` when import/distribution names differ or preparation is needed before execution. Bare or explicitly selected environments use preinstalled packages."#;

pub(super) const PYTHON_BRIDGE: &str = " With both runtimes and their bridge available, read R globals and call functions through `r.name`.";

pub(super) const PYTHON_SQL: &str = r#" Select a user-owned DB-API connection for SQL cells with `console_sql_connection(connection)`; restore managed DuckDB with `console_sql_connection(None)`."#;

pub(super) const PYTHON_SQL_R: &str =
    " With R-owned DuckDB, bind Python frames to an R name before querying.";

pub(super) const PYTHON_SQL_CONNECTION: &str = r#" Without R, `sql_connection()` returns the Python-owned connection; register frames with `sql_connection().register(name, frame)` (globals are not scanned)."#;

pub(super) const PYTHON_SQL_CONNECTION_SELECTED: &str = r#" Managed `sql_connection()` is available only when Python owns the provider; register frames with `sql_connection().register(name, frame)` (globals are not scanned). Visible fields do not determine ownership."#;

pub(super) const PYTHON_PLOTS: &str = r#" At cell end, even after errors, open `matplotlib.pyplot` figures return once as PNGs and close; `show()` is optional. Closing a figure suppresses capture."#;

pub(super) const PYTHON_R_PLOTS: &str = " R plots through `r` follow the R plot rules.";

pub(super) const CONTROL_START: &str =
    "Apply control before same-call input/code. `interrupt` requests ";

pub(super) const UNIX_INTERRUPT: &str =
    "SIGINT from the active host resolver or a cooperative interrupt from the worker";

pub(super) const WINDOWS_INTERRUPT: &str =
    "termination of the active host resolver or a cooperative interrupt from the worker";

pub(super) const CONTROL_END: &str = r#", preserving live state. A following cell runs only if prior work stops. `restart` discards live interpreter, database, debugger, and unread-input state; same-call stdin/code go only to the replacement. Requirements resolve before replacement; resolution failure preserves the current worker."#;

pub(super) const CONTROL_END_SELECTED: &str = r#", preserving live state. A following cell runs only if prior work stops. `restart` discards live state, including debugger and unread input; same-call stdin/code go only to the replacement. Requirements resolve before replacement; resolution failure preserves the current worker."#;

pub(super) const SQL_PROVIDER_SELECTED: &str = "SQL uses its configured provider without a setup cell. Provider choice is independent of visible language fields; missing-provider notices identify required preparation.";

pub(super) const SQL_RUNTIME: &str = r#"One complete SQL cell through the active connection. Managed DuckDB keeps a persistent catalog. Results are bounded table previews that abbreviate long text cells; query focused subsets or aggregates. The selected driver supplies its own SQL dialect and types."#;

pub(super) const SQL_R_FRAMES: &str = r#" With R-owned managed DuckDB, query R data frames by name; DuckDB tables/views take precedence."#;

pub(super) const SQL_R_STATEMENTS: &str =
    " Use DBI from an R cell for commands requiring the statement interface.";

pub(super) const TIMEOUT_SELECTED: &str = r#"Wait budget in milliseconds (default 60,000); omit for ordinary calls. `0` starts background work without waiting for completion. Expiry returns available output without cancelling execution, startup, or resolution. This is not a whole-call deadline: explicit preparation and lifecycle control may exceed it; standalone preparation is not limited by it."#;

pub(super) const SQL_OPERATIONS: &str = r#" Managed conveniences/extensions apply only to DuckDB. For SQLite, use an available sqlite extension with `ATTACH 'path' AS name (TYPE sqlite, READ_ONLY)`. With a sandbox, attach existing DuckDB databases outside writable paths using `ATTACH 'path' AS name (READ_ONLY)`. Use `SHOW TABLES`, `DESCRIBE`, `SUMMARIZE`, or `EXPLAIN`; CLI dot commands are not supported."#;

pub(super) const STDIN_START: &str =
    "Send stdin alone to answer an active read, prompt, or debugger. ";

pub(super) const STDIN_SELECTED: &str = STDIN_START;

pub(super) const STDIN_ORDERING: &str = r#"UTF-8 text is queued exactly; no newline is added, so line input normally needs a trailing `\n`. Empty text queues nothing. With a cell, input queues before code; an already waiting read may consume it first, including while interrupted work unwinds. Unread input can satisfy later reads; restart discards it."#;
