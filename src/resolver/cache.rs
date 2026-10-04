//! Host cache selection shared by resolver grants and retained runtime paths.

use crate::settings::SandboxSettings;
use serde_json::Value;
use std::path::PathBuf;

pub(crate) fn isolated_defaults(mut settings: SandboxSettings) -> Result<SandboxSettings, String> {
    // An explicit filesystem policy owns its cache grants and locations.
    if settings
        .get("filesystem")
        .and_then(|value| value.get("entries"))
        .is_some()
    {
        return Ok(settings);
    }
    let home = environment_path(&settings, "HOME").ok_or("resolver sandbox requires HOME")?;
    if !home.is_absolute() {
        return Err("resolver sandbox requires an absolute HOME".into());
    }
    let root = environment_path(&settings, "XDG_CACHE_HOME")
        .filter(|path| path.is_absolute())
        .unwrap_or_else(|| home.join(".cache"))
        .join("mcp-console/resolver/payload");
    let mut defaults = serde_json::Map::new();
    for (name, directory) in [
        ("UV_CACHE_DIR", "uv/cache"),
        ("UV_PYTHON_INSTALL_DIR", "uv/python"),
        ("UV_TOOL_DIR", "uv/tools"),
        ("IR_CACHE_DIR", "ir"),
        ("R_USER_CACHE_DIR", "r"),
        ("RENV_PATHS_ROOT", "renv"),
        ("PKG_CACHE_DIR", "pak"),
        ("MPLCONFIGDIR", "matplotlib"),
        (
            crate::local_runtime::DUCKDB_EXTENSION_DIRECTORY,
            "extensions",
        ),
    ] {
        let path = environment_path(&settings, name).unwrap_or_else(|| root.join(directory));
        defaults.insert(
            name.into(),
            path.to_str()
                .ok_or("resolver policy paths must be UTF-8")?
                .into(),
        );
    }
    let environment = settings
        .entry("environment")
        .or_insert_with(|| Value::Object(Default::default()));
    let environment = environment
        .as_object_mut()
        .ok_or("resolver.environment must be a mapping")?;
    for (name, value) in defaults {
        environment.entry(name).or_insert(value);
    }
    Ok(settings)
}

fn environment_path(settings: &SandboxSettings, name: &str) -> Option<PathBuf> {
    let inherit = settings.get("inherit_environment") != Some(&Value::Bool(false));
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
}

pub(crate) fn duckdb_extension_directory(
    settings: &SandboxSettings,
) -> Result<Option<PathBuf>, String> {
    if let Some(path) = environment_path(settings, crate::local_runtime::DUCKDB_EXTENSION_DIRECTORY)
    {
        return std::path::absolute(path)
            .map(Some)
            .map_err(|error| error.to_string());
    }
    let home = environment_path(settings, "HOME");
    #[cfg(windows)]
    let home = home.or_else(|| environment_path(settings, "USERPROFILE"));
    Ok(home
        .filter(|home| home.is_absolute())
        .map(|home| home.join(".duckdb/extensions")))
}

pub(crate) fn writable_roots(settings: &SandboxSettings) -> Result<Vec<PathBuf>, String> {
    // Cache selection uses the resolver's effective environment, including its
    // own trusted YAML overrides. Worker environment settings do not reach here.
    // Config-file cache paths need explicit resolver grants; this covers only
    // default locations and direct environment overrides.
    let env = |name| environment_path(settings, name);
    let home = env("HOME").ok_or("resolver sandbox requires HOME")?;
    if !home.is_absolute() {
        return Err("resolver sandbox requires an absolute HOME".into());
    }
    let xdg_cache = env("XDG_CACHE_HOME").unwrap_or_else(|| home.join(".cache"));
    let uv_cache_base = if xdg_cache.is_absolute() {
        xdg_cache.clone()
    } else {
        home.join(".cache")
    };
    let xdg_data = env("XDG_DATA_HOME")
        .filter(|path| path.is_absolute())
        .unwrap_or_else(|| home.join(".local/share"));
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
        env("UV_CACHE_DIR").unwrap_or_else(|| uv_cache_base.join("uv")),
        env("UV_PYTHON_INSTALL_DIR").unwrap_or_else(|| xdg_data.join("uv/python")),
        env("UV_TOOL_DIR").unwrap_or_else(|| xdg_data.join("uv/tools")),
        env("IR_CACHE_DIR").unwrap_or_else(|| r_cache.join("R/ir")),
        r_cache.join("R/reticulate"),
        duckdb_extension_directory(settings)?.expect("absolute resolver HOME"),
        env("MPLCONFIGDIR").unwrap_or_else(|| xdg_cache.join("matplotlib")),
        env("RENV_PATHS_ROOT").unwrap_or_else(|| r_cache.join("R/renv")),
        r_cache.join("R/pkgcache"),
    ];
    if cfg!(target_os = "macos")
        && ["UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_TOOL_DIR"]
            .into_iter()
            .any(|name| env(name).is_none())
    {
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
    Ok(caches)
}
