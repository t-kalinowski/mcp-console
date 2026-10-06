//! Named prose sections; selection belongs to the captured presentation profile.

pub(super) const BUILTIN_SCOPE: &str = r#"Persistent R, Python, and SQL workbench for exact computation, file and data inspection, transformation, visualization, statistics, simulation, and modeling. State persists across calls."#;

pub(super) const INTERFACE: &str =
    r#"Language fields describe the configured interface, not installed runtimes. "#;

pub(super) const SHARING: &str = r#"With both runtimes and their bridge available, Python reads R globals through `r.name` and R reads Python globals through `py$name`. "#;

pub(super) const MANAGED_SQL_SHARING: &str = r#"R-owned managed DuckDB SQL can query R data frames by name; without R, Python-owned DuckDB requires explicit frame registration with `sql_connection().register(name, frame)`. R accesses its SQL connection through `sql_connection()`; R or Python can select a user-owned connection with `console_sql_connection(connection)`. "#;

pub(super) const MANAGED_PREPARATION: &str = r#"Managed dependency preparation requires resolver support on the execution host; bare runtimes require preinstalled packages, and explicitly selected Python uses its preinstalled Python packages."#;

pub(super) const SEND_ORDERING: &str = r#" cell per call. Code-bearing calls must be sequential; a control-only interrupt may overlap a pending `send`. Inspect intermediate results before submitting dependent cells. Cells are not transactional; changes made before an error may remain."#;

pub(super) const POLLING: &str = r#"Omit code to poll, supply stdin, control the session, or prepare requirements when available. If a response ends in `[running; poll with an empty send]`, call `send` again without code or stdin; do not resubmit the cell. Send `stdin` alone to answer an active prompt or debugger. Field descriptions specify preparation, control, and timeout ordering."#;

pub(super) const OUTPUT: &str = r#"Each result has at most 8 KiB of UTF-8 text, including notices; oversized output keeps its beginning and latest tail. Images have separate limits. Retained raw-log paths are relative to the server's launch directory for project recordings and absolute for home recordings. Full retained text requires filesystem access there through existing tools; Console provides no read/search interface."#;

pub(super) const CUSTOM_SCOPE: &str = r#"Persistent custom-worker workbench. Language fields describe the configured interface; supported languages, evaluation, display, SQL, and cross-language sharing depend on the selected worker. Console does not supply built-in runtime packages, automatic import hooks, or a default SQL connection to custom workers. Managed requirements require execution-host resolver support and compatible worker preparation callbacks; Python requirements are unavailable with a custom worker."#;

pub(super) const CUSTOM_SWITCHING: &str =
    r#" Switch languages when useful, using capabilities supplied by the worker."#;

pub(super) const WINDOWS_SCOPE: &str = r#"Persistent R and Python workbench for local execution on Windows. State persists across calls. Enabled runtimes initialize in the background and can run without the other installed. With both runtimes and reticulate available, Python reads R globals through r.name and R can use reticulate to access Python. Managed R and Python requirements are prepared by ir and uv on the host. Explicit Python selections use preinstalled packages. SQL is not yet supported."#;

pub(super) const SQL_SELECTION: &str = r#"For databases and structured files, consider DuckDB SQL first for schema inspection, filtering, joins, aggregation, and nested JSON extraction. "#;

pub(super) const R_SELECTION: &str =
    r#"Use R for vectorized data and string operations, statistics, and plots. "#;

pub(super) const PYTHON_SELECTION: &str =
    r#"Use Python when its libraries or format-specific parsing simplify the task. "#;

pub(super) const SWITCHING: &str = r#"Switch languages when useful, reusing persistent state."#;

pub(super) const SQL_FILES: &str =
    r#"DuckDB can query CSV, Parquet, JSON, and JSONL directly; JSON support is built in. "#;

pub(super) const SQL_DEFAULTS: &str = r#"Built-in managed defaults include SQLite when dependency preparation is available; sessions without DuckDB preparation require preinstalled extensions. "#;

pub(super) const SQL_SQLITE: &str = r#"For SQLite, use an available sqlite extension and attach the database read-only with `ATTACH 'path' AS name (TYPE sqlite, READ_ONLY)`. "#;

pub(super) const SQL_EXTENSIONS: &str = r#"When preparation is supported, prepare additional extensions with `requirements={"action":"add","duckdb":["fts"]}`. "#;

pub(super) const SQL_RESULTS: &str = r#"SQL results include bounded table previews that abbreviate long text cells; return focused queries and summaries for inspection."#;

pub(super) const CUSTOM_R: &str = r#"One complete R cell, if supported by the custom worker. Evaluation, display, packages, graphics, and bridges are supplied by that worker. Omit for polling or stdin-only calls."#;

pub(super) const CUSTOM_PYTHON: &str = r#"One complete Python cell, if supported by the custom worker. Evaluation, display, packages, graphics, and bridges are supplied by that worker. Omit for polling or stdin-only calls."#;

pub(super) const CUSTOM_SQL: &str = r#"One complete SQL cell, if supported by the custom worker. Its selected connection supplies the dialect, packages, and result display. Console does not create a default database for a custom worker. Omit for polling or stdin-only calls."#;

pub(super) const R_RUNTIME: &str = r#"One complete R cell evaluated in persistent global state. Prefer Console for R execution,
including tests and package checks. When a fresh session is needed and existing in-memory
state can be discarded, send `control: "restart"` and `r` together; the code runs in the new
worker. For background execution, use `timeout_ms: 0`, then poll with an empty `send`.
Avoid `callr` merely to obtain fresh state or nonblocking execution. Use a subprocess when
the task requires separate process isolation, preserving the current session while running
independently, or ordinary R behavior without Console's runtime hooks.

The cell's final visible expression autoprints through R's normal console display; R also
autoprints earlier visible top-level expressions. Leave the primary result last and print
only when additional output is needed.
When dynamic resolution is available, the built-in worker resolves missing plain CRAN
package names on demand through `library()`, `require()`, `requireNamespace()`,
`loadNamespace()`, `::`, or `:::`. Use packages directly; do not probe package availability
or call `install.packages()`. Resolution makes a package available but attaches it only
through the original `library()` or `require()` call. In a bare runtime, packages must
already be installed and these operations keep their ordinary R behavior. R source is not
scanned in advance."#;

pub(super) const R_BRIDGE: &str = r#" When both runtimes and their bridge are available, read Python globals
through `py$name`."#;

pub(super) const R_SQL: &str = r#" With R-owned managed DuckDB active, R data frames are directly queryable
by name from later SQL cells. `sql_connection()` returns the R-owned SQL connection for DBI or dplyr use. Select a user-owned DBI connection for later SQL
cells with `console_sql_connection(connection)` and restore managed DuckDB with
`console_sql_connection(NULL)`. Do not disconnect the managed DuckDB connection, and restore a
selected connection before disconnecting it."#;

pub(super) const R_PLOTS: &str = r#" Default-device plots return as PNG images. Keep
all drawing operations for one plot in the same cell. Set persistent dimensions with
`options(console.plot.width_in = ..., console.plot.height_in = ..., console.plot.dpi = ...)`;
width and height are in inches. Omit this field for polling or stdin-only calls."#;

pub(super) const PYTHON_RUNTIME: &str = r#"One complete Python cell evaluated in persistent `__main__` state. Its final visible expression
autoprints through Python's normal display hook. Leave the primary result last and print only
when additional output is needed. When dynamic resolution is available and an import is
missing, the built-in managed worker resolves a PyPI distribution on demand, using a curated mapping for well-known
import/distribution differences and otherwise assuming the distribution matches the top-level
module. Python source is not scanned; resolution starts only when execution reaches the import.
Use `requirements.python` when the distribution differs from the inferred name, exact registry
metadata is needed, or the package should be prepared before the cell. A user-selected Python
environment or bare runtime disables both automatic resolution and managed requirements;
import packages already installed there directly."#;

pub(super) const PYTHON_BRIDGE: &str = r#" When both runtimes and their bridge are
available, read R globals and call R functions through `r.name`."#;

pub(super) const PYTHON_SQL: &str = r#" Select a user-owned DB-API
connection for later SQL cells with `console_sql_connection(connection)` and restore managed DuckDB with
`console_sql_connection(None)`."#;

pub(super) const PYTHON_SQL_R: &str = r#" With R-owned DuckDB, bind Python data frames to an R name
before querying them."#;

pub(super) const PYTHON_SQL_CONNECTION: &str = r#" Without R, `sql_connection()` returns the Python-owned connection;
register frames explicitly with `sql_connection().register(name, frame)`."#;

pub(super) const PYTHON_PLOTS: &str = r#" At cell end,
including after a Python error, every open `matplotlib.pyplot` figure returns once as a PNG
image and is closed.
`show()` is optional."#;

pub(super) const PYTHON_R_PLOTS: &str = r#" R plots called through `r` follow the R plot rules."#;

pub(super) const PYTHON_END: &str = r#" Omit this field for
polling or stdin-only calls."#;

pub(super) const CONTROL_START: &str = r#"Applies lifecycle control alone or before compatible same-call fields. `interrupt` requests
"#;

pub(super) const UNIX_INTERRUPT: &str = "SIGINT from the active host resolver or live worker";

pub(super) const WINDOWS_INTERRUPT: &str =
    "termination of the active host resolver or a cooperative interrupt from the live worker";

pub(super) const CONTROL_END: &str = r#" and preserves in-memory state. After
successful delivery, stdin is queued and `send` waits 100 milliseconds before observing the
earlier evaluation or attempting an optional following cell; the cell is not run if the
interrupted evaluation remains active. When `requirements` is available, restart resolves
same-call requirements before replacement. It then discards R, Python, DuckDB, debugger,
and unread-stdin state and sends same-call stdin and code only to the replacement."#;

pub(super) const MANAGED_SQL_R: &str = r#"R-owned managed DuckDB SQL can query R data frames by name. R accesses its SQL connection through `sql_connection()` and can select a user-owned connection with `console_sql_connection(connection)`. "#;

pub(super) const MANAGED_SQL_PYTHON: &str = r#"Without R, Python-owned DuckDB requires explicit frame registration with `sql_connection().register(name, frame)`. Python can select a user-owned connection with `console_sql_connection(connection)`. "#;

pub(super) const SQL_PROVIDER: &str = r#"SQL uses its configured provider without a setup cell. Hidden R or Python runtimes can implement SQL. Keep `requirements.r` and `requirements.python` available for provider preparation; a missing-provider diagnostic identifies the required package and restart. "#;

pub(super) const WINDOWS_PREPARATION: &str = r#" Managed R and Python requirements are prepared by ir and uv on the host. Explicit Python selections use preinstalled packages. SQL is not yet supported."#;

pub(super) const SQL_RUNTIME: &str = r#"One complete SQL cell evaluated through the active connection. The managed DuckDB backend is
used by default when its adapter and packages are available and keeps a persistent catalog.
A result with columns returns a bounded preview."#;

pub(super) const SQL_R_FRAMES: &str = r#" With R-owned managed DuckDB, an unqualified
relation name can query a data frame in R global state, and a DuckDB table or view with the
same name takes precedence."#;

pub(super) const SQL_DRIVERS: &str = r#" A user-selected R connection receives cells through `DBI::dbSendQuery()`; a Python DB-API connection executes
them through its connection or cursor protocol. The selected driver supplies its own SQL
dialect and type mappings."#;

pub(super) const SQL_R_STATEMENTS: &str = r#" Use DBI from an R cell for commands that require the statement
interface."#;

pub(super) const SQL_PYTHON_FRAMES: &str = r#" Without R, managed DuckDB uses Python and requires explicit frame registration
through `sql_connection().register(name, frame)`; it does not scan Python globals."#;

pub(super) const SQL_OPERATIONS: &str = r#"
Managed DuckDB conveniences and extension requirements apply only to the managed
backend. With the sandbox enabled, use `ATTACH 'path' AS name (READ_ONLY)` for existing DuckDB
databases outside the sandbox's writable paths; the sandbox blocks DuckDB's
default writable mode for those paths. Use `SHOW TABLES`, `DESCRIBE`, `SUMMARIZE`, and `EXPLAIN`
for DuckDB discovery. DuckDB CLI dot commands are not supported. Omit this field for polling
or stdin-only calls."#;

pub(super) const STDIN_START: &str = r#"Input for an active read, prompt, or debugger. When responding to active input, omit R, Python,
and SQL code and send stdin on its own. "#;

pub(super) const STDIN_SELECTED: &str = "Input for an active read, prompt, or debugger. When responding to active input, omit code and send stdin on its own. ";

pub(super) const STDIN_ORDERING: &str = r#"Its UTF-8 encoding is queued exactly; no newline is added.
Line-oriented input therefore normally needs a trailing `\n`. On a code-bearing call without
control, available requirements are prepared before nonempty stdin is queued. Standalone
preparation cannot queue nonempty stdin. After `interrupt`, nonempty stdin is queued before the
100-millisecond grace and may be consumed while the earlier operation unwinds. After `restart`,
same-call stdin is sent only to the replacement. When sent with a cell, nonempty text is queued
before the code is run; an already waiting interactive read may consume it before the new cell
begins. Empty text queues nothing. If output ends in `[waiting for stdin]`, send the requested
input here. Unread text can satisfy later reads and is discarded by restart."#;
