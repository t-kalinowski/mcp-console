//! One native boundary for sans-R preparation, separate from worker permissions.

use super::*;
use std::process::Command;

pub(super) struct Preparation {
    pub(super) storage: PathBuf,
    runner: PathBuf,
    policy: String,
}

impl Preparation {
    pub(super) fn capture(
        policy: Option<&crate::settings::SandboxSettings>,
        explicit_uv: Option<&OsStr>,
    ) -> Result<(Self, PathBuf, BTreeMap<OsString, OsString>), String> {
        let workspace = std::env::current_dir()
            .and_then(|path| path.canonicalize())
            .map_err(|error| format!("cannot locate preparation workspace: {error}"))?;
        let home = std::env::var_os("HOME").ok_or("managed Python requires HOME")?;
        let storage = PathBuf::from(home).join(".cache/mcp-console/python");
        std::fs::create_dir_all(&storage)
            .map_err(|error| format!("cannot create Python preparation storage: {error}"))?;
        let storage = storage.canonicalize().map_err(|error| error.to_string())?;
        if storage.starts_with(&workspace) || workspace.starts_with(&storage) {
            return Err("managed Python storage must be outside the workspace; set python in .agents/console/config.yaml to use an existing environment".into());
        }
        let select = |path: PathBuf| {
            path.canonicalize()
                .ok()
                .filter(|path| path.is_file() && !path.starts_with(&workspace))
        };
        let uv = match explicit_uv.filter(|value| *value != OsStr::new("managed")) {
            Some(path) => select(PathBuf::from(path)).ok_or("selected uv is in the project or unavailable")?,
            None => std::env::var_os("PATH").into_iter().flat_map(|path| std::env::split_paths(&path).collect::<Vec<_>>())
                .find_map(|directory| select(directory.join("uv")))
                .ok_or("R is unavailable and no protected `uv` executable was found on PATH; install uv or set python in .agents/console/config.yaml to select an existing environment")?,
        };
        let mut environment = BTreeMap::new();
        // Capture values, not configuration files or arbitrary build commands.
        // uv interprets the supported options itself. The legacy index alias
        // supplies a default only when its current spelling is absent.
        for name in [
            "UV_DEFAULT_INDEX",
            "UV_EXTRA_INDEX_URL",
            "UV_INDEX_STRATEGY",
            "UV_EXCLUDE_NEWER",
        ] {
            if let Some(value) = std::env::var_os(name).or_else(|| {
                (name == "UV_DEFAULT_INDEX")
                    .then(|| std::env::var_os("UV_INDEX_URL"))
                    .flatten()
            }) {
                if matches!(name, "UV_DEFAULT_INDEX" | "UV_EXTRA_INDEX_URL") {
                    for url in value
                        .to_str()
                        .ok_or("Python index must be UTF-8")?
                        .split_whitespace()
                    {
                        let parsed = pep508_rs::VerbatimUrl::parse_url(url)
                            .map_err(|error| error.to_string())?;
                        if !matches!(parsed.scheme(), "http" | "https") {
                            return Err("managed Python indexes must use HTTP or HTTPS".into());
                        }
                    }
                }
                environment.insert(name.into(), value);
            }
        }
        for (name, value) in [
            ("PATH", "/usr/bin:/bin"),
            ("UV_NO_CONFIG", "1"),
            ("UV_NO_BUILD", "1"),
            ("UV_PYTHON_PREFERENCE", "only-managed"),
            ("UV_KEYRING_PROVIDER", "disabled"),
        ] {
            environment.insert(name.into(), value.into());
        }
        for (name, relative) in [
            ("HOME", "home"),
            ("UV_CACHE_DIR", "uv"),
            ("UV_PYTHON_INSTALL_DIR", "python"),
            ("XDG_CACHE_HOME", "cache"),
        ] {
            let path = storage.join(relative);
            std::fs::create_dir_all(&path).map_err(|error| error.to_string())?;
            environment.insert(name.into(), path.into());
        }
        let (runner, policy) =
            crate::target_launch::preparation_sandbox(policy, &workspace, &storage, &uv)?;
        Ok((
            Self {
                storage,
                runner,
                policy,
            },
            uv,
            environment,
        ))
    }

    pub(super) fn command(
        &self,
        program: &Path,
        environment: &BTreeMap<OsString, OsString>,
        status: &Path,
    ) -> Command {
        let mut command = crate::resolver::process::resolver_command(&self.runner);
        command.env_clear().envs(environment).env("MCP_CONSOLE_PREPARATION_POLICY", &self.policy)
            .current_dir(&self.storage)
            .env("MCP_CONSOLE_RESOLVER_STATUS", status)
            .args(["--config-env", "MCP_CONSOLE_PREPARATION_POLICY", "--", "/bin/sh", "-c",
                r#""$@"; result=$?; printf '%s' "$result" > "$MCP_CONSOLE_RESOLVER_STATUS"; exit 0"#,
                "console-preparation"]).arg(program);
        command
    }
}
