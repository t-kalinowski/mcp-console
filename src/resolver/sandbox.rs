//! Resolver policy is independent of worker grants and materialized on its host.

use crate::settings::SandboxSettings;
use serde_json::{Value, json};
use std::process::Command;

pub(crate) fn command(settings: SandboxSettings, parent: u32) -> Result<Command, String> {
    let mut settings = materialize(settings)?;
    let directory =
        super::cache::duckdb_extension_directory(&settings)?.expect("absolute resolver HOME");
    crate::settings::preserve_environment(
        &mut settings,
        [(
            crate::local_runtime::DUCKDB_EXTENSION_DIRECTORY.as_ref(),
            Some(directory.as_os_str()),
        )],
    )?;
    let executable = std::env::current_exe().map_err(|error| error.to_string())?;
    let mut command = Command::new(&executable);
    command
        .args(["sandbox", "--exit-with-parent"])
        .arg(parent.to_string())
        .args(["--settings-env", crate::settings::ENVIRONMENT, "--"])
        .arg(executable)
        .env(
            crate::settings::ENVIRONMENT,
            serde_json::to_string(&settings).map_err(|error| error.to_string())?,
        )
        .env(crate::local_runtime::DUCKDB_EXTENSION_DIRECTORY, directory);
    Ok(command)
}

fn materialize(mut settings: SandboxSettings) -> Result<SandboxSettings, String> {
    let workspace = std::env::current_dir().map_err(|error| error.to_string())?;
    // Explicit entries already define the write grants, including Console caches.
    let caches = if settings
        .get("filesystem")
        .is_some_and(|filesystem| filesystem.get("entries").is_some())
    {
        Vec::new()
    } else {
        super::cache::writable_roots(&settings)?
    };
    let filesystem = settings.entry("filesystem").or_insert_with(|| json!({}));
    if let Value::Object(filesystem) = filesystem {
        filesystem.entry("kind").or_insert("restricted".into());
        if !filesystem.contains_key("entries") {
            let mut entries = vec![json!({
                "path": {"type": "special", "value": {"kind": "root"}}, "access": "read"
            })];
            for path in caches {
                let path = workspace.join(path);
                let path_text = path.to_str().ok_or("resolver policy paths must be UTF-8")?;
                // Linux cannot bind an absent writable root. Prepare default
                // cache directories on the host, including on cold starts.
                std::fs::create_dir_all(&path).map_err(|error| {
                    format!("cannot create resolver cache '{}': {error}", path.display())
                })?;
                entries
                    .push(json!({"path": {"type": "path", "path": path_text}, "access": "write"}));
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
            "releases.astral.sh",
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
