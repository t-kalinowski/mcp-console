//! Tool prose derived only from captured launch configuration.
use std::sync::Arc;

use rmcp::handler::server::router::tool::ToolRouter;

use super::ConsoleServer;

// Internal eval configuration; intentionally not exposed through the CLI.
pub(super) const LANGUAGES_ENV: &str = "MCP_CONSOLE_LANGUAGES";

#[derive(Clone, Copy, Default)]
pub(super) struct Languages {
    r: bool,
    python: bool,
    sql: bool,
}

impl Languages {
    pub(super) fn from_environment() -> Result<Self, String> {
        let Some(value) = std::env::var_os(LANGUAGES_ENV) else {
            return Ok(Self::all());
        };
        let value = value
            .into_string()
            .map_err(|_| Self::invalid_configuration())?;
        let mut languages = Self::default();
        for language in value.split(',') {
            match language {
                "r" => languages.r = true,
                "python" => languages.python = true,
                "sql" if !cfg!(windows) => languages.sql = true,
                _ => return Err(Self::invalid_configuration()),
            }
        }
        Ok(languages)
    }

    fn all() -> Self {
        Self {
            r: true,
            python: true,
            sql: !cfg!(windows),
        }
    }

    pub(super) fn enables(self, language: crate::cell::Language) -> bool {
        match language {
            crate::cell::Language::R => self.r,
            crate::cell::Language::Python => self.python,
            crate::cell::Language::Sql => self.sql,
        }
    }

    pub(super) fn field(language: crate::cell::Language) -> &'static str {
        match language {
            crate::cell::Language::R => "r",
            crate::cell::Language::Python => "python",
            crate::cell::Language::Sql => "sql",
        }
    }

    fn invalid_configuration() -> String {
        let available = if cfg!(windows) {
            "`r` and `python`"
        } else {
            "`r`, `python`, and `sql`"
        };
        format!("`{LANGUAGES_ENV}` must be a comma-separated subset of {available}")
    }
}

impl ConsoleServer {
    pub(super) fn configured_tool_router(
        languages: Languages,
        builtin: bool,
        policy: &crate::settings::SandboxSettings,
        no_sandbox: bool,
        target: Option<&crate::settings::Target>,
    ) -> ToolRouter<Self> {
        let prepared = target.and_then(|target| match &target.compute {
            crate::settings::Compute::Docker(_) => Some("image"),
            crate::settings::Compute::DockerSandbox(_) => Some("template"),
            crate::settings::Compute::Host {} => None,
        });
        let mut router = Self::tool_router();
        let send = router
            .map
            .get_mut("send")
            .expect("send tool must be registered");
        let description = send
            .attr
            .description
            .as_mut()
            .expect("send tool must have a description")
            .to_mut();
        description.push_str("\n\n");
        description.push_str(&self::description(policy, no_sandbox, target));
        let schema = Arc::make_mut(&mut send.attr.input_schema);
        let properties = schema
            .get_mut("properties")
            .and_then(serde_json::Value::as_object_mut)
            .expect("send schema must have object properties");
        let control = properties
            .get_mut("control")
            .and_then(serde_json::Value::as_object_mut)
            .expect("send control schema must be an object");
        control.insert(
            "type".to_string(),
            serde_json::Value::String("string".to_string()),
        );
        if let Some(values) = control
            .get_mut("enum")
            .and_then(serde_json::Value::as_array_mut)
        {
            values.retain(|value| !value.is_null());
        }
        #[cfg(windows)]
        if let Some(description) = control.get_mut("description") {
            *description = description
                .as_str()
                .unwrap_or_default()
                .replace(
                    "SIGINT from the active host resolver or live worker",
                    "a cooperative interrupt from the live worker",
                )
                .into();
        }
        // Omission carries meaning for get/reset. Do not advertise payload defaults.
        for property in properties
            .get_mut("requirements")
            .and_then(|requirements| requirements.get_mut("properties"))
            .and_then(serde_json::Value::as_object_mut)
            .expect("requirements schema properties")
            .values_mut()
        {
            property
                .as_object_mut()
                .expect("requirement property schema")
                .remove("default");
        }
        // Keep the normal tool prose and nested requirements unchanged; evals
        // only need to project which direct code fields the client can call.
        for (field, enabled) in [
            ("r", languages.r),
            ("python", languages.python),
            ("sql", languages.sql),
        ] {
            if !enabled {
                properties.shift_remove(field);
            }
        }
        let mut guidance = String::new();
        if languages.sql {
            guidance.push_str("For databases and structured files, consider DuckDB SQL first for schema inspection, filtering, joins, aggregation, and nested JSON extraction. ");
        }
        if languages.r {
            guidance.push_str(
                "Use R for vectorized data and string operations, statistics, and plots. ",
            );
        }
        if languages.python {
            guidance.push_str(
                "Use Python when its libraries or format-specific parsing simplify the task. ",
            );
        }
        if [languages.r, languages.python, languages.sql]
            .into_iter()
            .filter(|enabled| *enabled)
            .count()
            > 1
        {
            guidance.push_str("Switch languages when useful, reusing persistent state.");
        }
        guidance.truncate(guidance.trim_end().len());
        if languages.sql {
            guidance.push_str("\n\nDuckDB can query CSV, Parquet, JSON, and JSONL directly; JSON support is built in. ");
            if builtin && prepared.is_none() {
                guidance.push_str("Built-in managed defaults include SQLite when dependency preparation is available; sessions without DuckDB preparation require preinstalled extensions. ");
            }
            guidance.push_str(r#"For SQLite, use an available sqlite extension and attach the database read-only with `ATTACH 'path' AS name (TYPE sqlite, READ_ONLY)`. When preparation is supported, prepare additional extensions with `requirements={"action":"add","duckdb":["fts"]}`. "#);
            guidance.push_str("SQL results include bounded table previews that abbreviate long text cells; return focused queries and summaries for inspection.");
        }
        *description = description.replacen(
            "State persists across calls. ",
            &format!("State persists across calls.\n\n{guidance}\n\n"),
            1,
        );
        if !builtin {
            configure_custom(description, properties);
        }
        if let Some(source) = prepared {
            configure_prepared(description, properties, source);
        }
        #[cfg(windows)]
        if builtin {
            configure_windows(description);
        }
        if prepared.is_some() {
            let requirements = properties
                .get_mut("requirements")
                .expect("requirements schema");
            *requirements = serde_json::json!({
                "type": ["object", "null"],
                "description": "Inspect the server's retained declaration with action=get. Preparation is unavailable for this target; the declaration is not an installed-package inventory.",
                "properties": {"action": {"type": "string", "enum": ["get"]}},
                "required": ["action"],
                "additionalProperties": false,
            });
        }
        router
    }
}

use crate::settings::{Compute, SandboxSettings, Target};
use serde_json::{Map, Value};

#[cfg(windows)]
fn configure_windows(description: &mut String) {
    let (_, remaining) = description
        .split_once("\n\nSend one complete")
        .expect("send description");
    *description = format!(
        "Persistent R and Python workbench for local unsandboxed execution on Windows. State persists across calls. Each runtime initializes on demand and can run without the other installed. With both runtimes and reticulate available, Python reads R globals through r.name and R can use reticulate to access Python. Managed R and Python requirements are prepared by ir and uv on the host. Explicit Python selections use preinstalled packages. SQL is not yet supported.\n\nSend one complete{remaining}"
    ).replace("`r`, `python`, or `sql`", "`r` or `python`");
}

fn configure_custom(description: &mut String, properties: &mut Map<String, Value>) {
    let (_, remaining) = description
        .split_once("\n\nSend one complete")
        .expect("send description");
    let switching = if ["r", "python", "sql"]
        .into_iter()
        .filter(|field| properties.contains_key(*field))
        .count()
        > 1
    {
        " Switch languages when useful, using capabilities supplied by the worker."
    } else {
        ""
    };
    *description = format!(
        "Persistent custom-worker workbench. Language fields describe the configured interface; supported languages, evaluation, display, SQL, and cross-language sharing depend on the selected worker. Console does not supply built-in runtime packages, automatic import hooks, or a default SQL connection to custom workers. Managed requirements require execution-host resolver support and compatible worker preparation callbacks; Python requirements are unavailable with a custom worker.{switching}\n\nSend one complete{remaining}"
    );
    for (field, text) in [
        (
            "r",
            "One complete R cell, if supported by the custom worker. Evaluation, display, packages, graphics, and bridges are supplied by that worker. Omit for polling or stdin-only calls.",
        ),
        (
            "python",
            "One complete Python cell, if supported by the custom worker. Evaluation, display, packages, graphics, and bridges are supplied by that worker. Omit for polling or stdin-only calls.",
        ),
        (
            "sql",
            "One complete SQL cell, if supported by the custom worker. Its selected connection supplies the dialect, packages, and result display. Console does not create a default database for a custom worker. Omit for polling or stdin-only calls.",
        ),
    ] {
        if let Some(property) = properties.get_mut(field) {
            property["description"] = text.into();
        }
    }
}

/// Prepared-target restrictions are known from configuration, independent of the probe.
fn configure_prepared(description: &mut String, properties: &mut Map<String, Value>, source: &str) {
    *description = description.replace("managed DuckDB", "Console-owned DuckDB").replace(
        "Managed dependency preparation requires resolver support on the execution host; bare runtimes require preinstalled packages, and explicitly selected Python uses its preinstalled Python packages.",
        "Dependency preparation is unavailable on this target.",
    ).replace(
        r#"When preparation is supported, prepare additional extensions with `requirements={"action":"add","duckdb":["fts"]}`. "#,
        "",
    );
    description.push_str(&format!(
        "\n\nRuntime availability depends on the configured {source}. All dependencies and DuckDB extensions must be preinstalled there; Console never invokes dependency resolvers or installs missing imports. Rebuild the {source} and start a new server session to change its runtime or packages. Plain worker restart retains the selected interpreter and creates fresh language state and an empty in-memory SQL catalog."
    ));
    for (field, text) in [
        (
            "r",
            "One complete R cell when R is available. Expressions display automatically; R plots return as PNG images. When both runtimes and their bridge are available, read Python globals through py$name. R-owned DuckDB can query R global data frames by name. sql_connection() returns the R-owned connection; console_sql_connection(connection) selects a user-owned DBI connection, and console_sql_connection(NULL) restores the Console-owned catalog. Missing packages report ordinary R errors; automatic package installation is unavailable. Omit for polling or stdin-only calls.",
        ),
        (
            "python",
            "One complete Python cell when Python is available. The final expression displays automatically. Use input() for managed stdin; Matplotlib plots return as PNG images when installed. When both runtimes and their bridge are available, read R globals through r.name. console_sql_connection(connection) selects a user-owned DB-API connection, and console_sql_connection(None) restores the Console-owned catalog. Without R, sql_connection() returns the active Python-owned connection. Missing imports report ordinary Python errors; automatic package installation is unavailable. Omit for polling or stdin-only calls.",
        ),
        (
            "control",
            "Applies lifecycle control alone or before compatible same-call fields. interrupt signals the live worker and preserves state; compatible following input is queued before the 100-millisecond interrupt grace. A following cell runs only after the earlier operation finishes. restart discards language objects, debugger state, unread stdin, and the in-memory SQL catalog, retains the captured image/template and interpreter, and sends same-call input and code only to the replacement worker. Dependency preparation is unavailable.",
        ),
        (
            "sql",
            "One complete SQL cell through the active R DBI or Python DB-API connection, depending on available runtimes and the selected connection. Console opens its in-memory DuckDB catalog lazily when the adapter and DuckDB are preinstalled. R-owned DuckDB can query R global data frames by name; without R, Python data frames require explicit registration with sql_connection().register(name, frame). console_sql_connection(connection) selects a user-owned connection; console_sql_connection(None) in Python or console_sql_connection(NULL) in R restores the Console-owned catalog without discarding it. A query with columns returns a bounded preview. Use the selected driver's SQL dialect; DuckDB CLI dot commands are unsupported. Extensions must be preinstalled; Console does not install extensions or resolve packages. Worker replacement resets the catalog. Omit for polling or stdin-only calls.",
        ),
    ] {
        if let Some(property) = properties.get_mut(field) {
            property["description"] =
                format!("{text} Dependencies must be preinstalled in the {source}.").into();
        }
    }
}

fn description(policy: &SandboxSettings, no_sandbox: bool, target: Option<&Target>) -> String {
    let kind = target.map(|target| match &target.compute {
        Compute::Host {} => "host",
        Compute::Docker(_) => "docker",
        Compute::DockerSandbox(_) => "docker_sandbox",
    });
    // SBX only accepts compute enforcement; settings validation rejects other providers.
    let remote = target.is_some_and(|target| {
        !target.is_local_host() && matches!(target.compute, Compute::Host {})
    });

    let files = match kind {
        Some("docker") => "container files",
        Some("docker_sandbox") => "VM files",
        _ => "host files",
    };
    let profile = policy.get("extends").and_then(serde_json::Value::as_str);
    let filesystem = policy
        .get("filesystem")
        .and_then(|filesystem| filesystem.get("kind"))
        .and_then(crate::settings::native_variant_name)
        .or_else(|| {
            (!policy.contains_key("filesystem") && profile.is_some()).then_some("restricted")
        });
    let network = policy
        .get("network")
        .and_then(crate::settings::native_variant_name)
        .or_else(|| (!policy.contains_key("network") && profile.is_some()).then_some("restricted"));
    let network_access = match (filesystem, network, policy.get("proxy")) {
        // The pinned runner enforces managed proxy routing even with network enabled.
        (_, _, Some(proxy)) if !proxy.is_null() => {
            "can access the network subject to the launcher's proxy settings"
        }
        (Some("restricted" | "unrestricted"), Some("enabled"), _) => {
            "can directly access the network"
        }
        (Some("restricted" | "unrestricted"), Some("restricted"), _) => {
            "cannot directly access the network"
        }
        _ => "has network access governed by the launcher's sandbox settings",
    };
    let sandbox_access = match filesystem {
        Some("restricted") if profile == Some(":workspace") => format!(
            "uses the native \":workspace\" profile: it can edit files beneath the fixed launch workspace, write in the worker's private temporary directory and to explicitly allowed paths, and {network_access}. The workspace's .git, .agents, .codex, and .claude paths are readable and protected from writes by default. Explicit native rules can override these defaults or restrict reads"
        ),
        Some("restricted") if profile == Some(":read-only") => format!(
            "uses the native \":read-only\" profile: it can read {files} subject to configured read restrictions, write in the worker's private temporary directory and to explicitly allowed paths, and {network_access}"
        ),
        Some("restricted") => format!(
            "can read {files}, {network_access}, and can write in the worker's private temporary directory and to paths explicitly allowed by the launcher"
        ),
        Some("unrestricted") => {
            format!("has unrestricted filesystem access and {network_access}")
        }
        _ => format!(
            "has filesystem access governed by the launcher's sandbox settings and {network_access}"
        ),
    };

    let mut description = match kind {
        Some("docker_sandbox") => "Evaluated code runs inside a Console-owned Docker Sandbox microVM, enforced by Docker Sandboxes and its current inherited machine/organization policy and host integrations. Both relay and worker run in the VM. Native filesystem, network, proxy, and metadata defaults do not apply. Writable shares can expose .git, .agents, and controller records. Provider rules can change during the session. --no-sandbox retains the microVM and cannot bypass Docker policy.".to_string(),
        Some("docker") if no_sandbox => "Evaluated code runs inside an owned Docker container without an inner native sandbox. Docker bind access, namespaces, bridge networking, and container retirement still apply.".to_string(),
        _ if no_sandbox => format!("Evaluated code runs without a sandbox, with {} permissions, including filesystem and network access. Dependency resolution, when available, may execute installation or build code; use only trusted dependencies.", if remote { "the remote account's" } else { "the server's" }),
        _ => format!("Evaluated code {sandbox_access}. Dependency resolution, when available, runs outside the sandbox and may execute installation or build code; use only trusted dependencies."),
    };
    if let Some(target) = target {
        // Only explicit placement fields belong in presentation, never discovered identities
        // or the command/environment/policy payload used to launch the target.
        if remote {
            let host = target.host();
            description.push_str(&format!("\n\nConfigured SSH host: {host:?}. "));
        } else {
            description.push_str("\n\nConfigured local transport. ");
        }
        if !target.is_local_host() {
            description.push_str(&format!(
                "Configured execution workspace: {:?}. ",
                target.workspace
            ));
        }
        match kind {
            Some("docker" | "docker_sandbox") => {
                let (identity, storage) = if kind == Some("docker_sandbox") { ("template", "VM") } else { ("image", "container") };
                description.push_str(&format!("Startup captures an immutable {identity} identity and runtime selection for subsequent generations. Use preinstalled runtime packages; dynamic package preparation is disabled even if ir or uv is installed. Records, output spools, and returned images are written by the controller beneath its existing project .agents/console directory or its Console home directory; declared shares can expose them to the worker. Restart discards files stored only in the {storage} and preserves shared files. Quarto exports execute recorded cells when rendered; prepare the target environment and files first."));
                if kind == Some("docker") {
                    description.push_str(" Docker uses ordinary bridge networking. Without a proxy, external-sandbox delegates filesystem and network enforcement to Docker: native filesystem entries and network: restricted add no restrictions in that mode.");
                }
            }
            _ if remote => description.push_str("Dependency capability is discovered there. When available, managed defaults and requested R, Python, and DuckDB dependencies are prepared outside the worker sandbox with the remote account's trusted setup permissions; bare runtimes require preinstalled packages. Records and returned images are saved on the controller beneath its existing project .agents/console directory or its Console home directory. Files created by code remain remote. The source-only Quarto export does not reproduce the remote filesystem."),
            _ => {},
        }
    }
    description
}
