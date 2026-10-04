//! Resolver policy is independent of worker grants and materialized on its host.

use crate::settings::SandboxSettings;
use serde_json::{Value, json};
use std::path::PathBuf;
use std::process::Command;

pub(crate) fn command(settings: SandboxSettings, parent: u32) -> Result<Command, String> {
    let executable = std::env::current_exe().map_err(|error| error.to_string())?;
    let mut command = Command::new(&executable);
    command
        .args(["sandbox", "--exit-with-parent"])
        .arg(parent.to_string())
        .args(["--settings-env", crate::settings::ENVIRONMENT, "--"])
        .arg(executable)
        .env(
            crate::settings::ENVIRONMENT,
            serde_json::to_string(&materialize(settings)?).map_err(|error| error.to_string())?,
        );
    Ok(command)
}

fn materialize(mut settings: SandboxSettings) -> Result<SandboxSettings, String> {
    let workspace = std::env::current_dir().map_err(|error| error.to_string())?;
    // Cache selection uses the resolver's effective environment, including its
    // own trusted YAML overrides. Worker environment settings do not reach here.
    let inherit = settings.get("inherit_environment") != Some(&Value::Bool(false));
    let env = |name: &str| -> Option<PathBuf> {
        settings
            .get("environment")
            .and_then(|values| values.get(name))
            .and_then(Value::as_str)
            .map(PathBuf::from)
            .or_else(|| {
                inherit
                    .then(|| std::env::var_os(name).map(PathBuf::from))
                    .flatten()
            })
            .filter(|path| !path.as_os_str().is_empty())
    };
    let home = env("HOME").ok_or("resolver sandbox requires HOME")?;
    if !home.is_absolute() {
        return Err("resolver sandbox requires an absolute HOME".into());
    }
    let xdg_cache = env("XDG_CACHE_HOME").unwrap_or_else(|| home.join(".cache"));
    let xdg_data = env("XDG_DATA_HOME").unwrap_or_else(|| home.join(".local/share"));
    let r_cache = env("R_USER_CACHE_DIR")
        .or_else(|| env("XDG_CACHE_HOME"))
        .unwrap_or_else(|| {
            if cfg!(target_os = "macos") {
                home.join("Library/Caches/org.R-project.R")
            } else {
                xdg_cache.clone()
            }
        });
    let mut caches = vec![
        env("UV_CACHE_DIR").unwrap_or_else(|| xdg_cache.join("uv")),
        env("UV_PYTHON_INSTALL_DIR").unwrap_or_else(|| xdg_data.join("uv/python")),
        env("UV_TOOL_DIR").unwrap_or_else(|| xdg_data.join("uv/tools")),
        env("IR_CACHE_DIR").unwrap_or_else(|| r_cache.join("R/ir")),
        r_cache.join("R/reticulate"),
        home.join(".duckdb/extensions"),
        env("MPLCONFIGDIR").unwrap_or_else(|| xdg_cache.join("matplotlib")),
        env("RENV_PATHS_ROOT").unwrap_or_else(|| r_cache.join("R/renv")),
        r_cache.join("R/pkgcache"),
    ];
    if cfg!(target_os = "macos") {
        // uv retains existing installations in its pre-XDG native locations.
        caches.extend([
            home.join("Library/Caches/uv"),
            home.join("Library/Application Support/uv"),
        ]);
    }
    for name in [
        "IR_LIBRARY_ROOT",
        "RENV_PATHS_CACHE",
        "RENV_PATHS_SOURCE",
        "RENV_PATHS_BINARY",
        "R_PKG_CACHE_DIR",
        "PKG_CACHE_DIR",
    ] {
        if let Some(path) = env(name) {
            caches.push(path);
        }
    }
    let filesystem = settings.entry("filesystem").or_insert_with(|| json!({}));
    if let Value::Object(filesystem) = filesystem {
        filesystem.entry("kind").or_insert("restricted".into());
        if !filesystem.contains_key("entries") {
            let mut entries = vec![json!({
                "path": {"type": "special", "value": {"kind": "root"}}, "access": "read"
            })];
            for path in caches {
                let path = workspace.join(path);
                // Linux cannot bind an absent writable root. Prepare default
                // cache directories on the host, including on cold starts.
                std::fs::create_dir_all(&path).map_err(|error| {
                    format!("cannot create resolver cache '{}': {error}", path.display())
                })?;
                entries.push(json!({"path": {"type": "path", "path": path}, "access": "write"}));
            }
            filesystem.insert("entries".into(), entries.into());
        }
        if let Some(Value::Array(entries)) = filesystem.get_mut("entries") {
            for entry in entries {
                if entry.pointer("/path/type").and_then(Value::as_str) == Some("path")
                    && let Some(Value::String(path)) = entry.pointer_mut("/path/path")
                {
                    *path = std::path::absolute(workspace.join(&*path))
                        .map_err(|error| error.to_string())?
                        .to_str()
                        .ok_or("resolver policy paths must be UTF-8")?
                        .into();
                }
            }
        }
    }
    settings.entry("network").or_insert("restricted".into());
    let default_proxy = {
        let domains = [
            "pypi.org",
            "files.pythonhosted.org",
            "astral.sh",
            "github.com",
            "api.github.com",
            "codeload.github.com",
            "raw.githubusercontent.com",
            "objects.githubusercontent.com",
            "release-assets.githubusercontent.com",
            "r-lib.github.io",
            "packagemanager.posit.co",
            "rspm-sync.rstudio.com",
            "bioconductor.posit.co",
            "bioconductor.org",
            "cran.r-project.org",
            "cloud.r-project.org",
            "cran.rstudio.com",
            "extensions.duckdb.org",
        ]
        .into_iter()
        .map(|host| (host.to_owned(), "allow".into()))
        .collect::<SandboxSettings>();
        json!({
            "enabled": true, "enableSocks5": true, "enableSocks5Udp": false,
            "allowUpstreamProxy": false, "dangerouslyAllowAllUnixSockets": false,
            "mode": "full", "domains": domains, "allowLocalBinding": true,
        })
    };
    let proxy = settings.entry("proxy").or_insert_with(|| json!({}));
    if let Value::Object(proxy) = proxy {
        for (name, value) in default_proxy.as_object().unwrap() {
            proxy.entry(name.clone()).or_insert_with(|| value.clone());
        }
    }
    if cfg!(target_os = "macos") {
        settings
            .entry("macos_seatbelt_profile_extension")
            .or_insert_with(|| include_str!("../sandbox/policy_extensions.sbpl").into());
    }
    Ok(settings)
}
