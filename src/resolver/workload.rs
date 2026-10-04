//! The entire executable preparation graph. This entry point is a sandbox
//! workload; none of these operations run in the server or resolver broker.
use crate::resolver::preparation::{Discovery, Mode, NativeDiscovery, Operation, Selections};
use crate::resolver::{self, ResolverStopHandle};
use std::ffi::{OsStr, OsString};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};

static ACTIVE: AtomicBool = AtomicBool::new(false);
static INTERRUPTED: AtomicBool = AtomicBool::new(false);
pub(super) fn active() -> bool {
    ACTIVE.load(Ordering::Relaxed)
}

pub(super) fn interrupted() -> bool {
    INTERRUPTED.load(Ordering::Relaxed)
}

extern "C" fn interrupt(_: libc::c_int) {
    INTERRUPTED.store(true, Ordering::Relaxed);
}

#[derive(serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Request {
    pub(super) version: u32,
    pub(super) context: Option<Context>,
    pub(super) operation: Operation,
}

#[derive(serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Response {
    pub(super) version: u32,
    pub(super) context: Option<Context>,
    pub(super) result: Result<serde_json::Value, String>,
}

pub(crate) fn run() -> Result<(), String> {
    ACTIVE.store(true, Ordering::Relaxed);
    // Direct-mode interruption belongs to the active interpreter. Keep this
    // coordinator alive to collect its result and prevent a later stage from
    // starting. resolver_command restores each child's default disposition.
    // Native mode retires the whole sandbox.
    if unsafe { libc::signal(libc::SIGINT, interrupt as *const () as libc::sighandler_t) }
        == libc::SIG_ERR
    {
        return Err(std::io::Error::last_os_error().to_string());
    }
    use std::io::{Read, Write};
    let mut bytes = Vec::new();
    std::io::stdin()
        .take((super::broker::LIMIT + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|e| e.to_string())?;
    if bytes.len() > super::broker::LIMIT {
        return Err("resolver request exceeds 1 MiB".into());
    }
    let request: Request = serde_json::from_slice(&bytes)
        .map_err(|e| format!("invalid resolver workload request: {e}"))?;
    if request.version != super::broker::VERSION {
        return Err("incompatible resolver workload protocol".into());
    }
    for name in [
        "HOME",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "MCP_CONSOLE_EXTENSION_DIRECTORY",
    ] {
        if let Some(path) = std::env::var_os(name) {
            std::fs::create_dir_all(path).map_err(|e| e.to_string())?;
        }
    }
    if let Some(payload) = std::env::var_os("MCP_CONSOLE_RESOLVER_PAYLOAD") {
        std::fs::create_dir_all(PathBuf::from(payload).join("r/bootstrap"))
            .map_err(|e| e.to_string())?;
    }
    let mut context = request.context;
    let result = if let Operation::Discover { mode, local } = request.operation {
        Context::discover(mode, local, &|_| Ok(())).and_then(|(selected, discovery)| {
            context = Some(selected);
            serde_json::to_value(discovery).map_err(|e| e.to_string())
        })
    } else {
        context
            .as_mut()
            .ok_or("resolver context is missing")?
            .execute(request.operation, &|_| Ok(()))
    };
    let response = Response {
        version: super::broker::VERSION,
        context,
        result,
    };
    let bytes = serde_json::to_vec(&response).map_err(|e| e.to_string())?;
    if bytes.len() > super::broker::LIMIT {
        return Err("resolver result exceeds 1 MiB".into());
    }
    std::io::stdout()
        .write_all(&bytes)
        .map_err(|e| e.to_string())
}

#[derive(Clone, serde::Serialize, serde::Deserialize)]
pub(super) struct Context {
    local: bool,
    mode: Mode,
    bootstrap: Option<resolver::ManagedRBootstrap>,
    r: Option<resolver::ManagedRResolverConfiguration>,
    python: resolver::ManagedPythonResolverConfiguration,
    rscript: Option<PathBuf>,
    managed_python: bool,
}

impl Context {
    fn discover(
        mode: Mode,
        local: bool,
        on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<(Self, Discovery), String> {
        let mode = if matches!(mode, Mode::Auto) {
            if crate::local_runtime::Selection::r_is_present() {
                Mode::R
            } else {
                Mode::PythonOnly
            }
        } else {
            mode
        };
        let configured_python = std::env::var_os("RETICULATE_PYTHON");
        let managed_python = !configured_python
            .as_deref()
            .is_some_and(|python| !python.is_empty() && python != OsStr::new("managed"));
        let configured_python = configured_python.and_then(|python| python.into_string().ok());
        if !matches!(mode, Mode::R) {
            let python =
                resolver::ManagedPythonResolverConfiguration::capture().without_r_bootstrap();
            let has_uv = python.has_uv();
            let native = if !local && matches!(mode, Mode::PythonOnly) {
                let (mut selection, mut managed) = crate::local_runtime::Selection::python_on_host(
                    configured_python.clone().map(OsString::from),
                    &python,
                    on_started,
                )?;
                if let Some(python) = &mut selection.python {
                    if let Some(managed) = &mut managed {
                        managed.set_native((*python.selected).clone());
                    }
                    if let Some(directory) = std::env::var_os("MCP_CONSOLE_EXTENSION_DIRECTORY") {
                        python.duckdb_extension_directory = Some(PathBuf::from(directory));
                    }
                }
                Some(NativeDiscovery {
                    selection,
                    python: managed,
                })
            } else {
                None
            };
            return Ok((
                Self {
                    local,
                    mode,
                    bootstrap: None,
                    r: None,
                    python,
                    rscript: None,
                    managed_python,
                },
                Discovery {
                    managed: false,
                    selections: Selections {
                        r_home: None,
                        python: configured_python,
                        native_python: None,
                    },
                    local_r_home_bytes: None,
                    local_has_uv: local.then_some(has_uv),
                    native,
                    protected: Vec::new(),
                    lease: None,
                    extension_directory: None,
                    matplotlib_cache: None,
                },
            ));
        }
        let python = resolver::ManagedPythonResolverConfiguration::capture();
        let (bootstrap, rscript) = resolver::discover(&python, on_started)?;
        let home = rscript
            .parent()
            .and_then(std::path::Path::parent)
            .ok_or("remote Rscript has no R home")?;
        #[cfg(windows)]
        let home = if home.file_name().is_some_and(|name| name == "bin") {
            home.parent().ok_or("Rscript has no R home")?
        } else {
            home
        };
        let discovery = Discovery {
            managed: bootstrap.is_some(),
            selections: Selections {
                r_home: Some(home.to_string_lossy().into_owned()),
                python: configured_python,
                native_python: None,
            },
            #[cfg(unix)]
            local_r_home_bytes: local.then(|| {
                use std::os::unix::ffi::OsStrExt;
                home.as_os_str().as_bytes().to_vec()
            }),
            #[cfg(windows)]
            local_r_home_bytes: None,
            local_has_uv: local.then(|| python.has_uv()),
            native: None,
            protected: Vec::new(),
            lease: None,
            extension_directory: None,
            matplotlib_cache: None,
        };
        Ok((
            Self {
                local,
                mode,
                bootstrap,
                r: None,
                python,
                rscript: Some(rscript),
                managed_python,
            },
            discovery,
        ))
    }

    fn execute(
        &mut self,
        operation: Operation,
        on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<serde_json::Value, String> {
        match operation {
            Operation::Discover { .. } => {
                Err("discovery is only valid when opening a resolver".into())
            }
            Operation::Bootstrap => {
                let bootstrap = self
                    .bootstrap
                    .as_ref()
                    .ok_or("remote dynamic environment resolution is unavailable")?;
                // Capture choices once; a failed selected bootstrap remains an error.
                if self.r.is_none() {
                    self.r = Some(bootstrap.prepare(&mut self.python, on_started)?);
                }
                Ok(serde_json::Value::Null)
            }
            Operation::R { requirements } => {
                let configuration = self
                    .r
                    .as_ref()
                    .ok_or("remote R bootstrap has not been prepared")?;
                let r = resolver::resolve_r_with(configuration, requirements, on_started)?;
                serde_json::to_value(r).map_err(|error| error.to_string())
            }
            Operation::ResolveRStandalone { requirements } => {
                let r = if let Some(configuration) = &self.r {
                    resolver::resolve_r_with(configuration, requirements, on_started)?
                } else {
                    resolver::resolve_r(requirements, on_started, |configuration| {
                        self.r = Some(configuration);
                    })?
                };
                self.rscript = Some(r.rscript().to_path_buf());
                serde_json::to_value(r).map_err(|error| error.to_string())
            }
            Operation::Python {
                requirements,
                r,
                selected_python,
            } => {
                let r =
                    r.map(|r| r.on_host(self.rscript.as_ref().expect("managed R has an Rscript")));
                self.prepare_uv(r.as_ref(), on_started)?;
                let mut python = resolver::resolve_python_manifest_for_remote(
                    requirements,
                    &self.python,
                    if self.local { None } else { r.as_ref() },
                    selected_python.as_deref(),
                    on_started,
                )?;
                python.set_native(crate::python::inspect_native(python.python(), on_started)?);
                serde_json::to_value(python).map_err(|error| error.to_string())
            }
            Operation::PythonVersion { constraints, r } => {
                let r =
                    r.map(|r| r.on_host(self.rscript.as_ref().expect("managed R has an Rscript")));
                self.prepare_uv(r.as_ref(), on_started)?;
                let version = match r.as_ref() {
                    Some(r) if !self.local => resolver::resolve_python_version_for_remote(
                        constraints,
                        &self.python,
                        r,
                        on_started,
                    )?,
                    _ => resolver::resolve_python_version(constraints, &self.python, on_started)?,
                };
                Ok(serde_json::Value::String(version))
            }
            Operation::InspectPython { executable } => {
                serde_json::to_value(crate::python::inspect_native(&executable, on_started)?)
                    .map_err(|error| error.to_string())
            }
            Operation::Uv { r } => {
                let r = r.on_host(self.rscript.as_ref().expect("managed R has an Rscript"));
                if let Some(uv) = self.python.selected_uv() {
                    return serde_json::to_value(uv).map_err(|error| error.to_string());
                }
                let configuration = self.r.as_ref().ok_or("R bootstrap has not been prepared")?;
                let uv = configuration.resolve_uv(&r, &self.python, on_started)?;
                self.python.set_resolved_uv(uv.clone());
                serde_json::to_value(uv).map_err(|error| error.to_string())
            }
            Operation::Duckdb { r, extensions } => {
                let r = r.on_host(self.rscript.as_ref().expect("managed R has an Rscript"));
                resolver::resolve_duckdb_extensions(&r, &extensions, on_started)?;
                Ok(serde_json::Value::Null)
            }
            Operation::DuckdbPython {
                python,
                extensions,
                extension_directory,
            } => {
                if !matches!(self.mode, Mode::PythonOnly) || !self.managed_python {
                    return Err("Python-backed DuckDB preparation requires managed Python".into());
                }
                resolver::resolve_python_duckdb_extensions(
                    &python,
                    &extensions,
                    &extension_directory,
                    on_started,
                )?;
                Ok(serde_json::Value::Null)
            }
        }
    }

    fn prepare_uv(
        &mut self,
        r: Option<&resolver::ManagedR>,
        on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<(), String> {
        if !self.managed_python {
            return Err("managed Python requirements are disabled because the session uses a user-selected Python environment".into());
        }
        if !self.python.has_uv() {
            let r = r.ok_or("Python sessions without R require `uv` on PATH; set python in .agents/console/config.yaml to use an existing environment")?;
            let configuration = self
                .r
                .as_ref()
                .ok_or("remote R bootstrap has not been prepared")?;
            let uv = configuration.resolve_uv(r, &self.python, on_started)?;
            self.python.set_resolved_uv(uv);
        }
        Ok(())
    }
}
