//! The entire executable preparation graph. This entry point is a sandbox
//! workload; none of these operations run in the server or resolver broker.
use crate::resolver::preparation::{Discovery, Operation, Selections};
use crate::resolver::{self, ResolverStopHandle};
use std::path::PathBuf;

static ACTIVE: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
pub(super) fn active() -> bool {
    ACTIVE.load(std::sync::atomic::Ordering::Relaxed)
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
    ACTIVE.store(true, std::sync::atomic::Ordering::Relaxed);
    // Direct-mode interruption belongs to the active interpreter. Keep this
    // coordinator alive to collect its result; resolver_command restores the
    // default disposition in each child. Native mode retires the whole sandbox.
    if unsafe { libc::signal(libc::SIGINT, libc::SIG_IGN) } == libc::SIG_ERR {
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
    let result = if matches!(request.operation, Operation::Discover) {
        Context::discover(&|_| Ok(())).and_then(|(selected, discovery)| {
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
    bootstrap: Option<resolver::ManagedRBootstrap>,
    r: Option<resolver::ManagedRResolverConfiguration>,
    python: resolver::ManagedPythonResolverConfiguration,
    #[serde(with = "super::data::path")]
    rscript: PathBuf,
    managed_python: bool,
}

impl Context {
    pub(super) fn discover(
        on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<(Self, Discovery), String> {
        let python = resolver::ManagedPythonResolverConfiguration::capture();
        if !crate::local_runtime::Selection::r_is_present() {
            let python = python.without_r_bootstrap();
            let configured = std::env::var_os("RETICULATE_PYTHON");
            let selection =
                crate::local_runtime::Selection::python(configured.clone(), &python, on_started)?;
            let managed = match &selection {
                crate::local_runtime::Selection::Python { managed, .. } => managed.clone(),
                _ => unreachable!(),
            };
            let discovery = Discovery {
                managed: false,
                direct_uv: python.has_uv(),
                selections: Selections {
                    r_home: None,
                    python: configured.map(|s| s.to_string_lossy().into_owned()),
                },
                runtime: Some(selection),
                python: managed,
                protected: Vec::new(),
                lease: None,
                extension_directory: None,
                matplotlib_cache: None,
            };
            return Ok((
                Self {
                    bootstrap: None,
                    r: None,
                    python,
                    rscript: PathBuf::new(),
                    managed_python: true,
                },
                discovery,
            ));
        }
        let (bootstrap, rscript) = resolver::discover(&python, on_started)?;
        let configured_python = std::env::var("RETICULATE_PYTHON").ok();
        let managed_python = !configured_python
            .as_ref()
            .is_some_and(|python| !python.is_empty() && python != "managed");
        let discovery = Discovery {
            managed: bootstrap.is_some(),
            direct_uv: python.has_uv(),
            runtime: Some(crate::local_runtime::Selection::R {
                home: rscript
                    .parent()
                    .and_then(std::path::Path::parent)
                    .ok_or("Rscript has no home")?
                    .to_owned(),
            }),
            python: None,
            protected: Vec::new(),
            lease: None,
            extension_directory: None,
            matplotlib_cache: None,
            selections: Selections {
                r_home: Some(
                    rscript
                        .parent()
                        .and_then(std::path::Path::parent)
                        .ok_or("remote Rscript has no R home")?
                        .to_string_lossy()
                        .into_owned(),
                ),
                python: configured_python,
            },
        };
        Ok((
            Self {
                bootstrap,
                r: None,
                python,
                rscript,
                managed_python,
            },
            discovery,
        ))
    }

    pub(super) fn execute(
        &mut self,
        operation: Operation,
        on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<serde_json::Value, String> {
        match operation {
            Operation::Discover => Err("discovery is only valid when opening a resolver".into()),
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
                if self.r.is_none() {
                    self.r = Some(
                        self.bootstrap
                            .as_ref()
                            .ok_or("R preparation is unavailable")?
                            .prepare(&mut self.python, on_started)?,
                    );
                }
                let configuration = self.r.as_ref().expect("prepared R bootstrap");
                let r = resolver::resolve_r_with(configuration, requirements, on_started)?;
                serde_json::to_value(r).map_err(|error| error.to_string())
            }
            Operation::Python { requirements, r } => {
                let r = r.map(|r| r.on_host(&self.rscript));
                self.prepare_uv(r.as_ref(), on_started)?;
                let mut python =
                    resolver::resolve_python_manifest(requirements, &self.python, on_started)?;
                python.set_native(crate::python::inspect_native(python.python(), on_started)?);
                serde_json::to_value(python).map_err(|error| error.to_string())
            }
            Operation::PythonVersion { constraints, r } => {
                let r = r.map(|r| r.on_host(&self.rscript));
                self.prepare_uv(r.as_ref(), on_started)?;
                resolver::resolve_python_version(constraints, &self.python, on_started)
                    .map(serde_json::Value::String)
            }
            Operation::Duckdb { r, extensions } => {
                let r = r.on_host(&self.rscript);
                resolver::resolve_duckdb_extensions(&r, &extensions, on_started)?;
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
            let r = r.ok_or("remote Python bootstrap requires managed R")?;
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
