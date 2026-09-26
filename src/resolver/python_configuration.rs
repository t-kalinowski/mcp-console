use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};
use std::path::{Path, PathBuf};
use std::sync::Arc;

mod managed;

#[derive(Clone)]
pub(crate) struct ManagedPythonResolverConfiguration {
    environment: Arc<BTreeMap<OsString, OsString>>,
    explicit_uv: Option<OsString>,
    reticulate_uv: Option<OsString>,
    uv: Option<OsString>,
    preparation: Option<Arc<managed::Preparation>>,
}

impl ManagedPythonResolverConfiguration {
    pub(crate) fn capture() -> Self {
        let mut environment = std::env::vars_os()
            .filter(|(name, _)| is_uv_environment_variable(name) && name != "UV_OFFLINE")
            .collect::<BTreeMap<_, _>>();
        normalize_python_preference(&mut environment);
        let explicit_uv = std::env::var_os("RETICULATE_UV");
        let reticulate_uv = explicit_uv
            .clone()
            .or_else(|| super::find_path_entry("uv").map(Into::into));
        let uv = reticulate_uv
            .as_ref()
            .filter(|uv| uv.as_os_str() != OsStr::new("managed"))
            .cloned();
        Self {
            environment: Arc::new(environment),
            explicit_uv,
            reticulate_uv,
            uv,
            preparation: None,
        }
    }

    pub(crate) fn without_r_bootstrap(
        mut self,
        policy: Option<&crate::settings::SandboxSettings>,
        on_started: &dyn Fn(super::ResolverStopHandle) -> Result<(), String>,
    ) -> Result<Self, String> {
        let (preparation, uv, environment) =
            managed::Preparation::capture(policy, self.explicit_uv.as_deref(), on_started)?;
        self.uv = Some(uv.into());
        self.reticulate_uv = self.uv.clone();
        self.environment = Arc::new(environment);
        self.preparation = Some(Arc::new(preparation));
        Ok(self)
    }

    pub(crate) fn sans_r(&self) -> bool {
        self.preparation.is_some()
    }

    pub(crate) fn preparation_directory(&self) -> Option<&Path> {
        self.preparation
            .as_ref()
            .map(|preparation| preparation.directory())
    }

    pub(crate) fn output_directory(&self) -> PathBuf {
        self.preparation
            .as_ref()
            .map_or_else(std::env::temp_dir, |preparation| {
                preparation.directory().to_owned()
            })
    }

    pub(crate) fn command(
        &self,
        program: &Path,
        resolver: &super::process::ResolverProcess,
    ) -> Result<std::process::Command, String> {
        match &self.preparation {
            Some(preparation) => preparation.command(program, &resolver.status_file()?),
            None => Ok(super::process::resolver_command(program)),
        }
    }

    pub(crate) fn ensure_safe_python_path(&self, path: &Path) -> Result<(), String> {
        if let Some(preparation) = &self.preparation {
            let resolved = path
                .canonicalize()
                .map_err(|error| format!("cannot resolve managed Python path: {error}"))?;
            if !preparation
                .storage
                .iter()
                .any(|root| resolved.starts_with(root))
            {
                return Err(format!(
                    "managed Python path is outside uv storage: {}",
                    path.display()
                ));
            }
        }
        Ok(())
    }

    pub(super) fn explicit_uv(&self) -> Option<&OsStr> {
        self.explicit_uv.as_deref()
    }

    pub(super) fn uv(&self) -> Result<&OsStr, String> {
        self.uv
            .as_deref()
            .ok_or_else(|| "managed Python resolver has no `uv` executable".to_string())
    }

    fn reticulate_uv(&self) -> Result<&OsStr, String> {
        self.reticulate_uv
            .as_deref()
            .or(self.uv.as_deref())
            .ok_or_else(|| "managed Python resolver has no reticulate `uv` selection".to_string())
    }

    pub(super) fn python_preference(&self) -> Option<&OsStr> {
        self.environment.iter().find_map(|(name, value)| {
            (name.as_os_str() == OsStr::new("UV_PYTHON_PREFERENCE")).then_some(value.as_os_str())
        })
    }

    pub(crate) fn has_uv(&self) -> bool {
        self.uv.is_some()
    }

    pub(crate) fn set_resolved_uv(&mut self, uv: impl Into<OsString>) {
        let uv = uv.into();
        if self.uv.is_none() {
            self.uv = Some(uv.clone());
        }
        if self.reticulate_uv.is_none() {
            self.reticulate_uv = Some(uv);
        }
    }

    fn configure_uv(&self, command: &mut std::process::Command, uv: &OsStr) {
        for (name, _) in std::env::vars_os().filter(|(name, _)| is_uv_environment_variable(name)) {
            command.env_remove(name);
        }
        command
            .envs(self.environment.iter())
            .env("RETICULATE_UV", uv)
            .env_remove("UV_OFFLINE");
    }

    pub(super) fn configure_uv_bootstrap(&self, command: &mut std::process::Command) {
        self.configure_uv(command, OsStr::new("managed"));
    }

    pub(super) fn configure_direct(
        &self,
        command: &mut std::process::Command,
    ) -> Result<(), String> {
        if self.sans_r() {
            return Ok(());
        }
        let uv = self.reticulate_uv()?;
        self.configure_uv(command, uv);
        if uv == OsStr::new("managed") {
            let executable = std::path::Path::new(self.uv()?);
            let root = executable
                .parent()
                .and_then(std::path::Path::parent)
                .ok_or_else(|| {
                    format!(
                        "reticulate managed `uv` executable has no cache root: `{}`",
                        executable.display()
                    )
                })?;
            command
                .env("UV_CACHE_DIR", root.join("cache"))
                .env("UV_PYTHON_INSTALL_DIR", root.join("python"));
        }
        Ok(())
    }
}

fn normalize_python_preference(environment: &mut BTreeMap<OsString, OsString>) {
    let managed_name = OsStr::new("UV_MANAGED_PYTHON");
    let system_name = OsStr::new("UV_NO_MANAGED_PYTHON");
    let managed = uv_flag_value(environment, managed_name);
    let system = uv_flag_value(environment, system_name);
    if managed == Some(false) {
        environment.remove(managed_name);
    }
    if system == Some(false) {
        environment.remove(system_name);
    }
    if environment.contains_key(OsStr::new("UV_PYTHON_PREFERENCE")) {
        return;
    }
    let (name, preference) = if managed == Some(true) && !environment.contains_key(system_name) {
        (managed_name, "only-managed")
    } else if system == Some(true) && !environment.contains_key(managed_name) {
        (system_name, "only-system")
    } else {
        return;
    };
    environment.remove(name);
    environment.insert(
        OsString::from("UV_PYTHON_PREFERENCE"),
        OsString::from(preference),
    );
}

fn uv_flag_value(environment: &BTreeMap<OsString, OsString>, name: &OsStr) -> Option<bool> {
    environment
        .get(name)
        .and_then(|value| value.to_str())
        .and_then(|value| match value.to_ascii_lowercase().as_str() {
            "1" | "true" | "t" | "yes" | "y" | "on" => Some(true),
            "0" | "false" | "f" | "no" | "n" | "off" => Some(false),
            _ => None,
        })
}

fn is_uv_environment_variable(name: &OsStr) -> bool {
    name.as_encoded_bytes().starts_with(b"UV_")
}
