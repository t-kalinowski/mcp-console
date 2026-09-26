//! Sans-R supports registry resolution with managed CPython, not uv projects.
//! Capture a bounded set of settings as values so subsequent preparations never
//! rediscover configuration files or execute environment-file startup hooks.

use super::*;

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Index {
    name: Option<String>,
    url: String,
    #[serde(default)]
    default: bool,
}

impl ManagedPythonResolverConfiguration {
    pub(super) fn capture_managed_settings(&self) -> Result<BTreeMap<OsString, OsString>, String> {
        let mut settings = BTreeMap::new();
        for path in self.config_files() {
            self.ensure_safe_python_path(&path)?;
            let path = resolve_path(&path)?;
            let source = std::fs::read_to_string(&path).map_err(|error| {
                format!("cannot read uv configuration {}: {error}", path.display())
            })?;
            let table: toml::Table = toml::from_str(&source).map_err(|error| {
                format!("cannot parse uv configuration {}: {error}", path.display())
            })?;
            for (key, value) in table {
                if key == "index" {
                    let indexes: Vec<Index> = value.try_into().map_err(|_| unsupported("index"))?;
                    for index in indexes.into_iter().rev() {
                        let url = index.name.map_or_else(
                            || index.url.clone(),
                            |name| format!("{name}={}", index.url),
                        );
                        if index.default {
                            settings.insert("UV_DEFAULT_INDEX".into(), url.into());
                        } else {
                            prepend(&mut settings, "UV_INDEX", &url);
                        }
                    }
                    continue;
                }
                let name = format!("UV_{}", key.replace('-', "_").to_ascii_uppercase());
                let value = match value {
                    toml::Value::String(value) => value,
                    toml::Value::Integer(value) => value.to_string(),
                    toml::Value::Boolean(value) => value.to_string(),
                    toml::Value::Array(values) if key == "extra-index-url" => values
                        .iter()
                        .map(|value| value.as_str().ok_or_else(|| unsupported(&key)))
                        .collect::<Result<Vec<_>, _>>()?
                        .join(" "),
                    _ => return Err(unsupported(&key)),
                };
                if key == "extra-index-url" {
                    prepend(&mut settings, &name, &value);
                } else {
                    settings.insert(name.into(), value.into());
                }
            }
        }
        for (name, value) in self.environment.iter() {
            if matches!(
                name.to_str(),
                Some(
                    "UV_CONFIG_FILE"
                        | "UV_NO_CONFIG"
                        | "UV_NO_SYSTEM_CONFIG"
                        | "UV_RUN_RECURSION_DEPTH"
                        | "UV_INTERNAL__PARENT_INTERPRETER"
                )
            ) {
                continue;
            }
            settings.insert(name.clone(), value.clone());
        }
        for (name, value) in &settings {
            validate_setting(
                name.to_str().ok_or("uv setting name is not UTF-8")?,
                value.to_str().ok_or("uv setting value is not UTF-8")?,
            )?;
        }
        for name in ["UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR"] {
            if let Some(path) = settings.get(OsStr::new(name)) {
                self.ensure_safe_python_path(Path::new(path))?;
                let path = resolve_path(Path::new(path))?;
                settings.insert(name.into(), path.into());
            }
        }
        // These determine uv's default storage locations before `cache dir` and
        // `python dir` can confirm the effective paths under the captured settings.
        for name in ["HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"] {
            if let Some(path) = std::env::var_os(name) {
                self.ensure_safe_python_path(Path::new(&path))?;
                settings.insert(name.into(), resolve_path(Path::new(&path))?.into());
            }
        }
        settings.insert("UV_NO_CONFIG".into(), "1".into());
        settings.insert("UV_PYTHON_PREFERENCE".into(), "only-managed".into());
        Ok(settings)
    }

    fn config_files(&self) -> Vec<PathBuf> {
        if let Some(path) = self.environment.get(OsStr::new("UV_CONFIG_FILE")) {
            return vec![PathBuf::from(path)];
        }
        if super::uv_flag_value(&self.environment, OsStr::new("UV_NO_CONFIG")) == Some(true) {
            return Vec::new();
        }
        let mut paths = Vec::new();
        if super::uv_flag_value(&self.environment, OsStr::new("UV_NO_SYSTEM_CONFIG")) != Some(true)
        {
            let roots = std::env::var_os("XDG_CONFIG_DIRS").unwrap_or_else(|| "/etc/xdg".into());
            if let Some(path) = std::env::split_paths(&roots)
                .map(|root| root.join("uv/uv.toml"))
                .chain([PathBuf::from("/etc/uv/uv.toml")])
                .find(|path| path.is_file())
            {
                paths.push(path);
            }
        }
        let root = std::env::var_os("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".config")));
        if let Some(path) = root
            .map(|root| root.join("uv/uv.toml"))
            .filter(|path| path.is_file())
        {
            paths.push(path);
        }
        paths
    }
}

fn prepend(settings: &mut BTreeMap<OsString, OsString>, name: &str, value: &str) {
    let value = settings.get(OsStr::new(name)).map_or_else(
        || value.to_owned(),
        |old| format!("{value} {}", old.to_string_lossy()),
    );
    settings.insert(name.into(), value.into());
}

fn unsupported(name: &str) -> String {
    format!(
        "unsupported managed Python setting `{name}`; use registry resolution with uv-managed Python, or set python in .agents/console/config.yaml to select an existing environment"
    )
}

fn validate_setting(name: &str, value: &str) -> Result<(), String> {
    match name {
        "UV_INDEX" | "UV_DEFAULT_INDEX" | "UV_INDEX_URL" | "UV_EXTRA_INDEX_URL" => {
            for url in value.split_whitespace() {
                let url = if matches!(name, "UV_INDEX" | "UV_DEFAULT_INDEX") {
                    url.split_once('=')
                        .filter(|(name, _)| !name.contains(':'))
                        .map_or(url, |(_, url)| url)
                } else {
                    url
                };
                network_index(name, url)?;
            }
        }
        "UV_PYTHON_PREFERENCE" if value == "only-managed" => {}
        "UV_KEYRING_PROVIDER" if value == "disabled" => {}
        "UV_NO_CACHE" if matches!(value, "0" | "false") => {}
        "UV_CACHE_DIR"
        | "UV_PYTHON_INSTALL_DIR"
        | "UV_PYTHON_DOWNLOADS"
        | "UV_INDEX_STRATEGY"
        | "UV_RESOLUTION"
        | "UV_PRERELEASE"
        | "UV_EXCLUDE_NEWER"
        | "UV_CONCURRENT_DOWNLOADS"
        | "UV_CONCURRENT_BUILDS"
        | "UV_CONCURRENT_INSTALLS"
        | "UV_HTTP_TIMEOUT"
        | "UV_HTTP_RETRIES"
        | "UV_NATIVE_TLS"
        | "UV_SYSTEM_CERTS"
        | "UV_NO_INDEX" => {}
        name if name.starts_with("UV_INDEX_")
            && (name.ends_with("_USERNAME") || name.ends_with("_PASSWORD")) => {}
        _ => return Err(unsupported(name)),
    }
    Ok(())
}

fn network_index(name: &str, value: &str) -> Result<(), String> {
    let url = pep508_rs::VerbatimUrl::parse_url(value).map_err(|_| unsupported(name))?;
    if !matches!(url.scheme(), "https" | "http") {
        return Err(unsupported(name));
    }
    Ok(())
}
