//! Python-only MCP capability projection. R-present schemas remain unchanged.

use serde_json::{Map, Value};

pub(super) fn configure(
    description: &mut String,
    properties: &mut Map<String, Value>,
    python_preparation: bool,
    sql: bool,
) {
    let remaining = description
        .split_once("\n\nSend one complete")
        .expect("shared send description")
        .1;
    let environment = if python_preparation {
        "Console manages Python through uv. Explicit requirements.python and requirements.duckdb prepare packages and extensions before the first worker starts or with control: restart. Changed requirements on a running worker require an explicit restart; already retained requirements are a no-op. Restart resets objects and retains the accepted environment."
    } else {
        "Python uses the environment selected at server startup; restart resets objects and retains that environment."
    };
    let introduction = if sql && python_preparation {
        "Persistent local Python and SQL workbench without R. State persists across calls. Managed SQL uses a lazy in-memory DuckDB connection. In Python, sql_connection() returns the active SQL connection; console_sql_connection(connection) selects a user-owned DB-API connection and console_sql_connection(None) restores managed DuckDB. Register Python data frames explicitly with sql_connection().register(name, frame). R cells, live requirements, and automatic package installation are unavailable in this session."
    } else if sql {
        "Persistent local Python and SQL workbench without R. State persists across calls. Managed SQL uses a lazy in-memory DuckDB connection. In Python, sql_connection() returns the active SQL connection; console_sql_connection(connection) selects a user-owned DB-API connection and console_sql_connection(None) restores managed DuckDB. Register Python data frames explicitly with sql_connection().register(name, frame). R cells, live requirements, DuckDB extension preparation, and automatic package installation are unavailable in this session."
    } else {
        "Persistent local Python workbench. State persists across calls. R and SQL cells, live requirements, and automatic package installation are unavailable in this session."
    };
    *description = format!("{introduction} {environment}\n\nSend one complete{remaining}");
    *description = description.replace(
        "`r`, `python`, or `sql` cell",
        if sql {
            "`python` or `sql` cell"
        } else {
            "`python` cell"
        },
    );
    let python_description = if sql {
        "One complete Python cell in persistent state. The final expression displays automatically. Use input() for managed stdin; Matplotlib plots return as PNG images when installed. Use packages already in the selected environment. sql_connection() returns the active SQL connection, and console_sql_connection(connection) selects a user-owned DB-API connection. R integration, live requirements, and automatic package installation are unavailable. Use control: restart with python to run in a fresh worker using the same environment, or timeout_ms: 0 then poll for background execution."
    } else {
        "One complete Python cell in persistent state. The final expression displays automatically. Use input() for managed stdin; Matplotlib plots return as PNG images when installed. Use packages already in the selected environment. R and SQL cells, live requirements, and automatic package installation are unavailable. Use control: restart with python to run in a fresh worker using the same environment, or timeout_ms: 0 then poll for background execution."
    };
    for (field, description) in [
        ("python", python_description),
        (
            "control",
            "Applies lifecycle control alone or before compatible same-call fields. interrupt requests SIGINT from the live worker and preserves Python state. After successful delivery, stdin is queued and send waits 100 milliseconds before observing the earlier evaluation or attempting an optional following cell; that cell is not run if the interrupted evaluation remains active. restart discards Python objects, debugger state, and unread stdin, retains the selected environment, and sends same-call stdin and code only to the replacement worker.",
        ),
        (
            "stdin",
            r"Input for an active read, prompt, or debugger. When responding to active input, omit code and send stdin on its own. Its UTF-8 encoding is queued exactly; no newline is added. Line-oriented input normally needs a trailing `\n`. After interrupt, nonempty stdin is queued before the 100-millisecond grace and may be consumed while the earlier operation unwinds. After restart, same-call stdin goes only to the replacement. When sent with a cell, nonempty text is queued before code runs. Empty text queues nothing. If output ends in [waiting for stdin], send the requested input here. Unread text can satisfy later reads and is discarded by restart.",
        ),
        (
            "timeout_ms",
            "Omit for normal calls and polls. Defaults to 60,000 milliseconds. This limits the wait after cell dispatch or attachment to an active evaluation and includes one automatic worker replacement attempt. Reaching the timeout does not cancel execution or startup. Inline control, interrupt grace, and restart happen before dispatch and may make the complete call take longer. Use 0 for background execution, then poll with an empty send. If a response ends with [running; poll with an empty send] or [worker starting], poll without resubmitting the cell.",
        ),
    ] {
        if let Some(property) = properties.get_mut(field) {
            property["description"] = description.into();
        }
    }
    if sql {
        properties.get_mut("sql").expect("SQL property")["description"] = if python_preparation {
            "One complete SQL cell through the active Python DB-API connection. Managed DuckDB opens lazily on the first SQL cell or sql_connection() call and retains an in-memory catalog until worker replacement. Python data frames require explicit registration through sql_connection().register(name, frame); automatic frame scanning is disabled. console_sql_connection(connection) selects a user-owned DB-API connection, and console_sql_connection(None) restores managed DuckDB without discarding its catalog. A query with columns returns a bounded preview. SQL errors leave the Python session usable. requirements.duckdb prepares named extensions before startup or explicit restart; LOAD runs later in the worker. SQL never installs them automatically through Console. If DuckDB is unavailable, add duckdb with requirements.python and control: restart or select a custom connection. R cells are unavailable."
        } else {
            "One complete SQL cell through the active Python DB-API connection. Managed DuckDB opens lazily on the first SQL cell or sql_connection() call and retains an in-memory catalog until worker replacement. Python data frames require explicit registration through sql_connection().register(name, frame); automatic frame scanning is disabled. console_sql_connection(connection) selects a user-owned DB-API connection, and console_sql_connection(None) restores managed DuckDB without discarding its catalog. A query with columns returns a bounded preview. SQL errors leave the Python session usable. If DuckDB is unavailable, select a custom connection or install DuckDB in the selected environment; managed sessions can add it with requirements.python and control: restart. R cells and DuckDB extension preparation are unavailable."
        }.into();
    }
    if python_preparation {
        let python_description = if sql {
            "One complete Python cell in persistent state. The final expression displays automatically. Use input() for managed stdin; Matplotlib plots return as PNG images when installed. sql_connection() returns the active SQL connection. requirements.python and requirements.duckdb prepare packages and extensions before first use, or with control: restart before the replacement runs this cell. Live requirement changes, automatic installation, and R integration are unavailable. Use timeout_ms: 0 then poll for background execution."
        } else {
            "One complete Python cell in persistent state. The final expression displays automatically. Use input() for managed stdin; Matplotlib plots return as PNG images when installed. requirements.python prepares packages before first use, or with control: restart before the replacement runs this cell. Live package updates, automatic package installation, R cells, and SQL cells are unavailable. Use timeout_ms: 0 then poll for background execution."
        };
        for (field, description) in [
            ("python", python_description),
            (
                "control",
                "Applies lifecycle control alone or before compatible same-call fields. interrupt requests SIGINT from the live worker and preserves Python state; during preparation it interrupts the host resolver. Same-call stdin is queued before the interrupt grace. Requirements with interrupt are rejected before signaling or queuing input. restart discards objects and unread stdin, then sends same-call stdin and code only to the replacement. The complete Python candidate is resolved and inspected before its DuckDB extensions are prepared and before stopping the current worker. Preparation failure preserves the current worker, retained requirements, and queued input; same-call code and stdin are not sent. Retirement or replacement failure follows the ordinary restart contract. Plain restart reuses the accepted environment.",
            ),
        ] {
            if let Some(property) = properties.get_mut(field) {
                property["description"] = description.into();
            }
        }
        let requirements = properties
            .get_mut("requirements")
            .expect("requirements schema");
        let cells = if sql { "Python or SQL" } else { "Python" };
        requirements["description"] = format!(
            "Inspect with action=get; add named Python packages and DuckDB extensions with action=add (the default), replace the declaration with action=set, or restore defaults with action=reset. Applies to a Console-managed uv environment. Supported before the first worker starts, alone or with a {cells} cell, and with control: restart, with or without code. Additions accumulate in the retained declaration, initially NumPy, pandas, and DuckDB but no extensions. An explicit set, including an empty set, remains exactly that declaration. A changed live environment requires restart; retained requirements are a no-op. The Python candidate is inspected before its extensions are prepared and before worker retirement. Automatic installation is unavailable. Standalone preparation cannot queue stdin."
        )
        .into();
        let fields = requirements["properties"]
            .as_object_mut()
            .expect("requirement properties");
        fields.shift_remove("r");
        fields.get_mut("duckdb").expect("DuckDB requirements")["description"] = "Validated DuckDB extension names for the managed connection, for example fts. The host runs the accepted Python environment's DuckDB installation API with its default repository and signature checks. Preparation does not LOAD extensions or run cells. Additions before first use or with control: restart are supported; changed live declarations require restart. DuckDB must be included in requirements.python when an exact set would otherwise omit it.".into();
        fields.get_mut("python").expect("Python requirements")["description"] = "Named PEP 508 requirements added to the retained Python manifest. Local paths, URLs, editable requirements, and archives are rejected as request values. Preparation runs outside the worker sandbox with full host permissions and may execute installation or build code. Use trusted dependencies and resolver configuration. Live changes require control: restart.".into();
    }
}
