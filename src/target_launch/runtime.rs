use serde_json::Value;
use std::path::PathBuf;
use std::process::Command;

/// Resolve image defaults and workload selectors on the target, never controller paths.
pub(crate) fn configure_runtime(
    command: &mut Command,
    policy: &crate::settings::SandboxSettings,
    compute: &str,
) -> Result<(), String> {
    for name in ["R_HOME", "RETICULATE_PYTHON"] {
        let value = policy
            .get("environment")
            .and_then(|env| env.get(name))
            .and_then(Value::as_str)
            .map(str::to_string)
            .or_else(|| std::env::var(name).ok());
        match value {
            Some(value)
                if name != "RETICULATE_PYTHON" || (!value.is_empty() && value != "managed") =>
            {
                command.env(name, value);
            }
            _ => {
                // Without an explicit R_HOME, the runtime probe discovers R
                // after the workload environment is applied.
                command.env_remove(name);
            }
        }
    }
    command
        .env("MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION", "0")
        .env("MCP_CONSOLE_EXECUTION_COMPUTE", compute)
        .env("RETICULATE_USE_MANAGED_VENV", "no")
        .env_remove("MCP_CONSOLE_MANAGED_PYTHON")
        .env_remove("MCP_CONSOLE_PYTHON_EXECUTABLE")
        .env_remove("MCP_CONSOLE_PREINSTALLED");
    Ok(())
}

pub(crate) fn runtime_probe() -> Result<(), String> {
    if let Some(home) = std::env::var_os("R_HOME") {
        let rscript = PathBuf::from(home).join("bin/Rscript");
        if !rscript.is_file() {
            return Err(format!(
                "R_HOME must select an existing R installation: {} is missing",
                rscript.display()
            ));
        }
    }
    // Prepared no-R targets use Python for both Python and managed SQL.
    // The probe captures R selection; the worker loads libR only on activation.
    let selected = std::env::var_os("RETICULATE_PYTHON");
    if selected
        .as_ref()
        .is_some_and(|python| !PathBuf::from(python).is_file())
    {
        return Err("container RETICULATE_PYTHON must select an existing interpreter".into());
    }
    let python = selected.unwrap_or_else(|| "python3".into());
    let output = Command::new(&python)
        .args([
            "-c",
            r#"import sys
if sys.version_info < (3, 10):
    sys.exit("MCP Console requires Python 3.10 or later")
"#,
        ])
        .output()
        .map_err(|error| format!("container Python probe failed: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "container Python probe failed with {}: {}",
            output.status,
            String::from_utf8_lossy(&output.stderr)
        ));
    }
    let r_home = if std::env::var_os("R_HOME").is_some()
        || std::env::var_os("PATH").is_some_and(|path| {
            std::env::split_paths(&path).any(|directory| directory.join("R").is_file())
        }) {
        Some(harp::command::r_home_setup().map_err(|error| error.to_string())?)
    } else {
        None
    };
    println!(
        "{}",
        serde_json::to_string(&crate::ssh::preparation::Selections {
            r_home: r_home.map(|home| home.to_string_lossy().into_owned()),
            python: Some(python.to_string_lossy().into_owned()),
        })
        .map_err(|error| error.to_string())?
    );
    Ok(())
}
