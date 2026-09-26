//! Captured uv configuration inside a native preparation boundary.

use super::*;
use std::process::{Command, Stdio};

pub(super) struct Preparation {
    temporary: crate::local_runtime::TemporaryDirectory,
    pub(super) storage: Vec<PathBuf>,
    runner: PathBuf,
    policy: serde_json::Value,
}

impl Preparation {
    pub(super) fn directory(&self) -> &Path {
        self.temporary.path()
    }

    pub(super) fn capture(
        policy: Option<&crate::settings::SandboxSettings>,
        explicit_uv: Option<&OsStr>,
        on_started: &dyn Fn(super::super::ResolverStopHandle) -> Result<(), String>,
    ) -> Result<(Self, PathBuf, BTreeMap<OsString, OsString>), String> {
        let workspace = std::env::current_dir()
            .and_then(|path| path.canonicalize())
            .map_err(|error| format!("cannot locate preparation workspace: {error}"))?;
        let temporary_parent = std::env::temp_dir()
            .canonicalize()
            .map_err(|error| error.to_string())?;
        if temporary_parent.starts_with(&workspace) {
            return Err("managed Python preparation temporary storage must be outside the workspace; set python in .agents/console/config.yaml to use an existing environment".into());
        }
        // Supported worker policies grant only the workspace and private temporary
        // storage. Deny its parent too, so preparation cannot consume worker files.
        let blocked = [&workspace, &temporary_parent];
        let temporary = crate::local_runtime::TemporaryDirectory::create()?;
        let (runner, mut native) = crate::target_launch::preparation_sandbox(
            policy,
            &workspace,
            temporary.path(),
            &temporary_parent,
        )?;
        let select = |path: PathBuf| {
            path.canonicalize()
                .ok()
                .filter(|path| path.is_file() && !blocked.iter().any(|root| path.starts_with(root)))
        };
        let uv = match explicit_uv.filter(|value| *value != OsStr::new("managed")) {
            Some(path) => select(PathBuf::from(path)).ok_or("selected uv is in the project or temporary storage, or unavailable; set python in .agents/console/config.yaml to use an existing environment")?,
            None => std::env::var_os("PATH").into_iter().flat_map(|path| std::env::split_paths(&path).collect::<Vec<_>>())
                .find_map(|directory| select(directory.join("uv")))
                .ok_or("R is unavailable and no protected `uv` executable was found on PATH; install uv or set python in .agents/console/config.yaml to select an existing environment")?,
        };
        if blocked.iter().any(|root| runner.starts_with(root)) {
            return Err("managed Python requires Console to be installed outside the project and worker temporary storage; set python in .agents/console/config.yaml to use an existing environment".into());
        }
        let mut environment = std::env::vars_os().collect::<BTreeMap<_, _>>();
        for name in ["VIRTUAL_ENV", "UV_MANAGED_PYTHON", "UV_NO_MANAGED_PYTHON"] {
            environment.remove(OsStr::new(name));
        }
        // uv owns configuration parsing and precedence. These are Console's
        // persistent, wheel-only, managed-interpreter lifecycle constraints.
        for (name, value) in [
            ("UV_NO_BUILD", "1"),
            ("UV_NO_CACHE", "0"),
            ("UV_NO_ENV_FILE", "1"),
            ("UV_NO_SOURCES", "1"),
            ("UV_PYTHON_PREFERENCE", "only-managed"),
            ("UV_WORKING_DIR", "/"),
            ("UV_PROJECT", "/"),
        ] {
            environment.insert(name.into(), value.into());
        }
        native["inherit_environment"] = false.into();
        crate::settings::preserve_environment(
            native.as_object_mut().expect("preparation policy"),
            environment
                .iter()
                .map(|(name, value)| (name.as_os_str(), Some(value.as_os_str()))),
        )?;
        let mut preparation = Self {
            temporary,
            storage: Vec::new(),
            runner,
            policy: native,
        };
        let resolver =
            super::super::process::ResolverProcess::for_preparation(Some(preparation.directory()))?;
        let mut on_started = Some(on_started);
        for (arguments, name) in [
            (["cache", "dir"], "UV_CACHE_DIR"),
            (["python", "dir"], "UV_PYTHON_INSTALL_DIR"),
        ] {
            let mut command = preparation.command(&uv, &resolver.status_file()?)?;
            command
                .args(arguments)
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped());
            let output = super::super::managed_python::run_resolver_command(
                command,
                &resolver,
                &mut on_started,
                &uv,
                "uv storage discovery",
            )?;
            if !output.status.success() {
                return Err(format!(
                    "uv storage discovery failed: {}; set python in .agents/console/config.yaml to use an existing environment",
                    String::from_utf8_lossy(&output.stderr).trim()
                ));
            }
            let path = PathBuf::from(
                String::from_utf8(output.stdout)
                    .map_err(|error| error.to_string())?
                    .trim(),
            );
            if !path.is_absolute() {
                return Err("uv returned a non-absolute storage directory".into());
            }
            std::fs::create_dir_all(&path).map_err(|error| error.to_string())?;
            let path = path.canonicalize().map_err(|error| error.to_string())?;
            if blocked
                .iter()
                .any(|root| path.starts_with(root) || root.starts_with(&path))
            {
                return Err("uv storage overlaps the workspace or worker temporary storage; set python in .agents/console/config.yaml to use an existing environment".into());
            }
            environment.insert(name.into(), path.clone().into());
            preparation.storage.push(path);
        }
        crate::target_launch::preparation_storage(&mut preparation.policy, &preparation.storage);
        crate::settings::preserve_environment(
            preparation
                .policy
                .as_object_mut()
                .expect("preparation policy"),
            environment
                .iter()
                .map(|(name, value)| (name.as_os_str(), Some(value.as_os_str()))),
        )?;
        Ok((preparation, uv, environment))
    }

    pub(super) fn command(&self, program: &Path, status: &Path) -> Result<Command, String> {
        let mut policy = self.policy.clone();
        policy["environment"]["MCP_CONSOLE_RESOLVER_STATUS"] = status
            .to_str()
            .ok_or("preparation status path must be UTF-8")?
            .into();
        let mut command = crate::resolver::process::resolver_command(&self.runner);
        // Loader and shell configuration must reach only the isolated target,
        // never the native launcher or its setup helpers.
        command.env_clear().env("MCP_CONSOLE_PREPARATION_POLICY", policy.to_string())
            .current_dir("/")
            .args(["--config-env", "MCP_CONSOLE_PREPARATION_POLICY", "--", "/bin/sh", "-c",
                r#"exec 9>"$MCP_CONSOLE_RESOLVER_STATUS"; "$@"; result=$?; printf '%s' "$result" >&9; exit 0"#,
                "console-preparation"]).arg(program);
        Ok(command)
    }
}
