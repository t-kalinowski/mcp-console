//! Tool prose derived from effective placement and enforcement metadata.
use crate::settings::SandboxSettings;
use serde_json::{Map, Value};

/// Prepared dependencies are an image capability for either runtime family.
pub(super) fn configure_prepared(
    description: &mut String,
    properties: &mut Map<String, Value>,
    kind: &str,
) {
    let source = if kind == "docker" {
        "image"
    } else {
        "template"
    };
    *description = description
        .replace(
            "Persistent local",
            if kind == "docker" {
                "Persistent Docker"
            } else {
                "Persistent Docker Sandbox"
            },
        )
        .replace("Managed SQL uses", "SQL uses")
        .replace(
            "managed DuckDB SQL",
            "SQL through the Console-owned DuckDB catalog",
        )
        .replace("managed DuckDB", "the Console-owned DuckDB catalog");
    description.push_str(&format!(
        "\n\nRuntimes are discovered inside the selected {source} during background preparation. All dependencies and DuckDB extensions must be preinstalled there; Console never invokes dependency resolvers or installs missing imports. Rebuild the {source} and start a new server session to change its runtime or packages. Plain worker restart retains the selected interpreter and creates fresh language state and an empty in-memory SQL catalog."
    ));
    for (field, text) in [
            (
                "r",
                "One complete R cell when R is installed in the target. Expressions display automatically; R plots return as PNG images. When the Python bridge is available, read Python globals through py$name. The R-backed Console-owned DuckDB catalog can query R global data frames by name. console_sql_connection(connection) selects a user-owned DBI connection, and console_sql_connection(NULL) restores the Console-owned catalog. Missing packages report ordinary R errors; automatic package installation is unavailable.",
            ),
            (
                "python",
                "One complete Python cell when Python is installed in the target. The final expression displays automatically. Use input() for managed stdin; Matplotlib plots return as PNG images when installed. When the R bridge is available, read R globals through r.name. console_sql_connection(connection) selects a user-owned DB-API connection, and console_sql_connection(None) restores the Console-owned catalog. Missing imports report ordinary Python errors; automatic package installation is unavailable.",
            ),
            (
                "control",
                "Applies lifecycle control alone or before compatible same-call fields. interrupt signals the live worker and preserves state; compatible following input is queued before the interrupt grace. A following cell runs only after the earlier operation finishes. restart discards language objects, debugger state, unread stdin, and the in-memory SQL catalog, retains the captured image/template and interpreter, and sends same-call input and code only to the replacement worker. Dependency preparation is unavailable.",
            ),
    ] {
        if let Some(property) = properties.get_mut(field) {
            property["description"] = text.into();
        }
    }
    for field in ["r", "python"] {
        if let Some(property) = properties.get_mut(field) {
            let text = property["description"]
                .as_str()
                .expect("language description");
            property["description"] =
                format!("{text} Dependencies must be preinstalled in the {source}.").into();
        }
    }
    if let Some(property) = properties.get_mut("sql") {
        property["description"] = format!(
            "One complete SQL cell through the active R DBI or Python DB-API connection. Console opens its in-memory DuckDB catalog during initialization when the adapter and DuckDB are preinstalled in the {source}. The default catalog uses R DBI when R is available, otherwise Python DB-API. The R-backed catalog can query R global data frames by name; Python data frames require explicit registration with sql_connection().register(name, frame). console_sql_connection(connection) selects a user-owned connection, and console_sql_connection(None) in Python or console_sql_connection(NULL) in R restores the Console-owned catalog without discarding it. Missing DuckDB leaves the installed interpreters and custom connections usable. A query with columns returns a bounded preview. Extensions load from the {source}'s cache; Console does not install extensions or resolve packages. Spill files and stored secrets use private disposable worker storage. Worker replacement resets the catalog."
        ).into();
    }
}

pub(super) fn description(
    policy: &SandboxSettings,
    no_sandbox: bool,
    target: Option<&Value>,
) -> String {
    let kind = target
        .and_then(|target| target.pointer("/compute/kind"))
        .and_then(Value::as_str);
    let compute_enforcement = target
        .and_then(|target| target.get("provider"))
        .and_then(Value::as_str)
        == Some("compute");

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
        Some("docker_sandbox") if compute_enforcement => "Evaluated code runs inside a Console-owned Docker Sandbox microVM, enforced by Docker Sandboxes and its current inherited machine/organization policy and host integrations. Both relay and worker run in the VM. Native filesystem, network, proxy, and metadata defaults do not apply. Writable shares can expose .git, .agents, and controller records. Provider rules can change during the session. --no-sandbox retains the microVM and cannot bypass Docker policy.".to_string(),
        Some("docker") if no_sandbox => "Evaluated code runs inside an owned Docker container without an inner native sandbox. Docker bind access, namespaces, bridge networking, and container retirement still apply.".to_string(),
        _ if no_sandbox => format!("Evaluated code runs without a sandbox, with {} permissions, including filesystem and network access. Dependency resolution, when available, may execute installation or build code; use only trusted dependencies.", if target.is_some() { "the remote account's" } else { "the server's" }),
        _ => format!("Evaluated code {sandbox_access}. Dependency resolution, when available, runs outside the sandbox and may execute installation or build code; use only trusted dependencies."),
    };
    if let Some(target) = target {
        description.push_str(&format!("\n\nExecution target: {target}. "));
        match kind {
            Some("docker" | "docker_sandbox") => {
                let (identity, storage) = if kind == Some("docker_sandbox") { ("template", "VM") } else { ("image", "container") };
                let packages = if target.pointer("/runtime/kind").and_then(Value::as_str) == Some("python") { "Python and SQL" } else { "R, Python, and SQL" };
                description.push_str(&format!("Each generation uses the captured immutable {identity} identity and runtime selection. Use preinstalled {packages} packages; dynamic package preparation is disabled even if ir or uv is installed. Records, output spools, and returned images are written by the controller beneath its existing project .agents/console directory or its Console home directory; declared shares can expose them to the worker. Restart discards files stored only in the {storage} and preserves shared files. Quarto exports execute recorded cells when rendered; prepare the target environment and files first."));
                if kind == Some("docker") {
                    description.push_str(" Docker uses ordinary bridge networking. Without a proxy, external-sandbox delegates filesystem and network enforcement to Docker: native filesystem entries and network: restricted add no restrictions in that mode.");
                }
            }
            _ => description.push_str("Dependency capability is discovered there. When available, managed defaults and requested R, Python, and DuckDB dependencies are prepared outside the worker sandbox with the remote account's trusted setup permissions; bare runtimes require preinstalled packages. Records and returned images are saved on the controller beneath its existing project .agents/console directory or its Console home directory. Files created by code remain remote. The source-only Quarto export does not reproduce the remote filesystem."),
        }
    }
    description
}
