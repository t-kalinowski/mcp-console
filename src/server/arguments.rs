use rmcp::schemars;
use serde::Deserialize;

const DEFAULT_TIMEOUT_MS: u64 = 60_000;

#[derive(Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub(super) struct SendArguments {
    #[schemars(description = super::presentation::r_description())]
    pub(super) r: Option<String>,
    #[schemars(description = super::presentation::python_description())]
    pub(super) python: Option<String>,
    /// One complete SQL cell evaluated through the active connection. The managed DuckDB backend is
    /// used by default when its adapter and packages are available and keeps a persistent catalog.
    /// A result with columns returns a bounded preview. With R-owned managed DuckDB, an unqualified
    /// relation name can query a data frame in R global state, and a DuckDB table or view with the
    /// same name takes precedence. A user-selected R connection receives cells through `DBI::dbSendQuery()`; a Python DB-API connection executes
    /// them through its connection or cursor protocol. The selected driver supplies its own SQL
    /// dialect and type mappings. Use DBI from an R cell for commands that require the statement
    /// interface. Without R, managed DuckDB uses Python and requires explicit frame registration
    /// through `sql_connection().register(name, frame)`; it does not scan Python globals.
    /// Managed DuckDB conveniences and extension requirements apply only to the managed
    /// backend. With the sandbox enabled, use `ATTACH 'path' AS name (READ_ONLY)` for existing DuckDB
    /// databases outside the sandbox's writable paths; the sandbox blocks DuckDB's
    /// default writable mode for those paths. Use `SHOW TABLES`, `DESCRIBE`, `SUMMARIZE`, and `EXPLAIN`
    /// for DuckDB discovery. DuckDB CLI dot commands are not supported. Omit this field for polling
    /// or stdin-only calls.
    pub(super) sql: Option<String>,
    #[schemars(description = super::presentation::control_description())]
    pub(super) control: Option<SendControl>,
    /// Inspect or manage retained R packages, Python packages, and DuckDB extensions.
    /// action=get returns a read-only snapshot, including Python constraints and separate runtime
    /// infrastructure. It cannot accompany code, stdin, control, or payload fields. The complete
    /// manifest is in structuredContent.requirements even when it exceeds the text preview limit.
    /// action=add is the default; action=set replaces the whole declaration without injecting defaults;
    /// action=reset restores startup defaults. Changed replacements require control="restart" with a
    /// live worker. Empty set means no optional requirements; bare {} is invalid.
    /// Requirements alone perform standalone preparation. With one cell, they are preconditions of
    /// that cell. With `control = "restart"`, they are part of the restart transaction, with or
    /// without a cell. Only add can accompany interrupt, and only when a cell follows; Python-only
    /// sessions reject requirements with interrupt before signaling or queuing input.
    /// Preparation does not import, attach, or load dependencies. On a code-bearing call without
    /// control, preparation completes before same-call nonempty stdin is queued. Standalone
    /// preparation cannot queue nonempty stdin. With restart, failure leaves the current worker
    /// unchanged and sends neither stdin nor code. With add, interrupt, and a following cell, signal
    /// delivery and stdin enqueue happen before requirements are validated or prepared and are not
    /// rolled back if that later work fails. Ordinary CRAN packages used by the built-in R worker need
    /// not be declared here; use `requirements.r` to stage packages ahead of evaluation or provide
    /// explicit `ir` references. In the built-in managed Python environment, missing imports normally
    /// resolve at runtime. Use `requirements.python` to stage a distribution before the cell, provide
    /// a version, extra, or marker, or correct automatic inference. Python source is not pre-scanned,
    /// and SQL does not trigger package discovery. A cell is not run if explicit preparation fails or
    /// further changes require restart. Resolution runs with server permissions and may download
    /// packages or extensions or execute installation or build code. Use only trusted requirements.
    pub(super) requirements: Option<Requirements>,
    /// Input for an active read, prompt, or debugger. When responding to active input, omit R, Python,
    /// and SQL code and send stdin on its own. Its UTF-8 encoding is queued exactly; no newline is added.
    /// Line-oriented input therefore normally needs a trailing `\n`. On a code-bearing call without
    /// control, available requirements are prepared before nonempty stdin is queued. Standalone
    /// preparation cannot queue nonempty stdin. After `interrupt`, nonempty stdin is queued before the
    /// 100-millisecond grace and may be consumed while the earlier operation unwinds. After `restart`,
    /// same-call stdin is sent only to the replacement. When sent with a cell, nonempty text is queued
    /// before the code is run; an already waiting interactive read may consume it before the new cell
    /// begins. Empty text queues nothing. If output ends in `[waiting for stdin]`, send the requested
    /// input here. Unread text can satisfy later reads and is discarded by restart.
    pub(super) stdin: Option<String>,
    /// Omit for normal calls and polls. This limits how long the tool waits; it returns immediately
    /// when execution completes. Reaching the timeout does not cancel execution. Use `0` to start
    /// background work, then poll with an empty `send`.
    ///
    /// Defaults to 60,000 milliseconds. One deadline starts at call entry and includes initial
    /// background startup, evaluation observation, and one automatic worker replacement attempt. It does not cancel resolution
    /// or startup. Inline control, interrupt grace, restart, and explicit requirement preparation happen
    /// before dispatch and may make the complete call take longer. This value does not limit standalone
    /// preparation. Automatic R and Python import
    /// resolution are part of the running evaluation and count toward this wait. On expiry, the call
    /// returns available output and a state marker, such as
    /// `[running; poll with an empty send]` or `[worker starting]`. If evaluation remains active, poll
    /// with an empty `send` call; do not resubmit the cell.
    #[serde(default = "default_timeout_ms")]
    pub(super) timeout_ms: u64,
}

#[derive(Clone, Copy, Deserialize, schemars::JsonSchema)]
#[schemars(inline)]
#[serde(rename_all = "snake_case")]
pub(super) enum SendControl {
    Interrupt,
    Restart,
}

#[derive(Deserialize, schemars::JsonSchema)]
#[schemars(inline)]
#[serde(deny_unknown_fields)]
pub(super) struct Requirements {
    /// get inspects the committed declaration without starting a worker or consuming output.
    /// add (default) accumulates requirements. set replaces all lists and Python constraints;
    /// omitted fields are empty, including when only action is supplied. reset restores startup
    /// defaults. get and reset reject payload fields. Changed set/reset with a live worker require
    /// control="restart"; the complete candidate resolves before the old worker is retired.
    /// add accepts up to 64 entries per language per call; set accepts the complete accumulated manifest.
    #[serde(default)]
    pub(super) action: crate::worker_client::RequirementsAction,
    /// Python version constraints, preserved by get and replaced as a whole by set. Add appends
    /// constraints; changing constraints with a live worker requires control="restart".
    #[serde(default, deserialize_with = "supplied_list")]
    #[schemars(with = "Vec<String>")]
    pub(super) python_version: Option<Vec<String>>,
    /// Python package publication cutoff accepted by uv, for example "2026-01-01". set clears an
    /// omitted or null cutoff; add preserves an omitted cutoff and cannot replace an existing one.
    #[serde(default, deserialize_with = "supplied_nullable")]
    pub(super) exclude_newer: Option<Option<String>>,
    /// DuckDB extension names for the managed DuckDB backend, for standalone preparation,
    /// preparation before a cell, or a restart transaction, for example `fts`, `spatial`, or `excel`.
    /// JSON and ICU are included in built-in defaults. Names must start with a lowercase ASCII
    /// letter and contain only lowercase ASCII letters, digits, and underscores. The host resolver
    /// uses DuckDB's own `INSTALL`, with DuckDB's default extension repository and
    /// native cache. Preparation does not load extension code; `LOAD` and automatic loading happen
    /// later inside the worker.
    #[serde(default, deserialize_with = "supplied_list")]
    #[schemars(with = "Vec<String>", inner(length(min = 1, max = 64)))]
    pub(super) duckdb: Option<Vec<String>>,
    /// Single-line `ir` package references for standalone preparation, preparation before a
    /// cell, or a restart transaction, for example `data.table`, `sf`, or `yaml12`. Use this field
    /// to stage packages ahead of evaluation or supply an explicit supported remote `ir` reference.
    /// Automatic R discovery accepts only plain package names. An idle worker that implements R
    /// preparation can add requirements without losing live state. Local package sources are
    /// rejected because resolution runs with server permissions.
    #[serde(default, deserialize_with = "supplied_list")]
    #[schemars(with = "Vec<String>", inner(length(min = 1)))]
    pub(super) r: Option<Vec<String>>,
    /// Named PEP 508 registry requirements for standalone preparation, preparation before a
    /// cell, or a restart transaction, for example `polars>=1`, `scikit-learn`, or
    /// `matplotlib; python_version >= '3.10'`. Use explicit requirements when automatic import
    /// inference needs a different distribution, a version, an extra, or an environment marker, or
    /// when the distribution should be prepared before the cell. Automatic imports infer bare
    /// distribution names only. Paths, file URLs, editable requirements, direct references, local
    /// archives, and local projects are rejected. Preparation does not import the package. An idle
    /// server-managed worker may activate compatible additions without losing state. A nonempty
    /// user-selected `RETICULATE_PYTHON` disables automatic resolution and managed Python
    /// requirements.
    #[serde(default, deserialize_with = "supplied_list")]
    #[schemars(with = "Vec<String>", inner(length(min = 1)))]
    pub(super) python: Option<Vec<String>>,
}

fn supplied_list<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<Option<Vec<String>>, D::Error> {
    Vec::<String>::deserialize(deserializer).map(Some)
}

fn supplied_nullable<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<Option<Option<String>>, D::Error> {
    Option::<String>::deserialize(deserializer).map(Some)
}

fn default_timeout_ms() -> u64 {
    DEFAULT_TIMEOUT_MS
}
