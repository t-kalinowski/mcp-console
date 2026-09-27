//! Unsandboxed, data-only owner of resolver policy, storage and native launches.

use super::{ResolverStopHandle, process::ResolverProcess};
use crate::resolver::preparation::{Discovery, Operation};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

pub(crate) use super::policy::{Launch, Settings, protect_worker};
pub(crate) use super::storage::inherit_lease;
pub(super) const VERSION: u32 = 1;
pub(super) const LIMIT: usize = 1024 * 1024;

pub(crate) struct Context {
    launch: Launch,
    executable: PathBuf,
    runner: Option<(PathBuf, u32)>,
    storage: Option<super::storage::Storage>,
    context: Option<super::workload::Context>,
}

impl Context {
    pub(crate) fn new(launch: Launch) -> Result<Self, String> {
        let executable = std::env::current_exe()
            .and_then(|p| p.canonicalize())
            .map_err(|e| e.to_string())?;
        let (storage, runner) = if launch.no_sandbox {
            (None, None)
        } else {
            let (runner, version) = crate::sandbox::resolver_runner()?;
            let runner = (runner.canonicalize().map_err(|e| e.to_string())?, version);
            let storage = launch
                .cache_home
                .as_deref()
                .map(|cache| super::storage::Storage::acquire(cache, &[&executable, &runner.0]))
                .transpose()?;
            (storage, Some(runner))
        };
        Ok(Self {
            launch,
            executable,
            runner,
            storage,
            context: None,
        })
    }

    pub(crate) fn discover(
        &mut self,
        started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<Discovery, String> {
        let mut discovery: Discovery = if self.launch.custom_worker {
            Discovery {
                managed: true,
                direct_uv: false,
                selections: Default::default(),
                runtime: None,
                python: None,
                protected: Vec::new(),
                lease: None,
                extension_directory: None,
                matplotlib_cache: None,
            }
        } else {
            let value = self.execute(Operation::Discover, started)?;
            serde_json::from_value(value).map_err(|e| format!("invalid resolver discovery: {e}"))?
        };
        discovery.protected.clear();
        discovery.lease = None;
        discovery.extension_directory = None;
        discovery.matplotlib_cache = None;
        if !self.launch.no_sandbox {
            discovery.protected.push(
                self.executable
                    .parent()
                    .and_then(Path::parent)
                    .ok_or("Console has no installation prefix")?
                    .to_owned(),
            );
        }
        if let Some(storage) = &self.storage {
            discovery.protected.push(storage.root.clone());
            discovery.lease = Some(storage.lease_path());
            discovery.extension_directory = Some(storage.payload.join("extensions"));
            discovery.matplotlib_cache = Some(storage.payload.join("matplotlib"));
        }
        Ok(discovery)
    }

    pub(crate) fn execute(
        &mut self,
        operation: Operation,
        started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<serde_json::Value, String> {
        if self.context.is_none() && !matches!(operation, Operation::Discover) {
            self.execute(Operation::Discover, started)?;
        }
        let request = super::workload::Request {
            version: VERSION,
            context: self.context.clone(),
            operation,
        };
        let bytes = serde_json::to_vec(&request).map_err(|e| e.to_string())?;
        if bytes.len() > LIMIT {
            return Err("resolver request exceeds 1 MiB".into());
        }
        let mut command;
        let resolver = if !self.launch.no_sandbox {
            let storage = self.storage.as_ref().ok_or("sandboxed dependency preparation requires XDG_CACHE_HOME or HOME for owned resolver storage")?;
            let (runner, version) = self.runner.as_ref().expect("native resolver runner");
            let policy = self
                .launch
                .native(storage, &self.executable, runner, *version)?;
            command = Command::new(runner);
            command
                .env_clear()
                .current_dir("/")
                .env("MCP_CONSOLE_RESOLVER_POLICY", policy.to_string())
                .args(["--config-env", "MCP_CONSOLE_RESOLVER_POLICY", "--"])
                .arg(&self.executable)
                .arg("resolver-workload");
            ResolverProcess::native()
        } else {
            command = super::process::resolver_command(&self.executable);
            command
                .env_clear()
                .envs(&self.launch.environment)
                .current_dir(&self.launch.workspace)
                .arg("resolver-workload");
            ResolverProcess::new()
        };
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        crate::process_descriptors::close_unlisted_from_multithreaded_parent(&mut command)?;
        if let Some(storage) = &self.storage {
            inherit_lease(&mut command, &storage.lease_path())?;
        }
        // Retain the previous prepared state if an operation fails or is cancelled.
        let (mut child, stdout, stderr) = resolver
            .spawn(&mut command)
            .map_err(|e| format!("cannot launch resolver workload: {e}"))?;
        let stdin = child.stdin.take().expect("resolver stdin");
        if let Err(error) = started(resolver.stop_handle()) {
            resolver.abort(&mut child, &self.executable, "dependency preparation")?;
            return Err(error);
        }
        let output = resolver.wait(
            &mut child,
            super::process::write_input(stdin, bytes),
            stdout,
            stderr,
            &self.executable,
            "dependency",
        )?;
        if !output.stderr.is_empty() {
            eprint!("{}", String::from_utf8_lossy(&output.stderr));
        }
        if resolver.stop_handle().control_outcome()
            == Some(super::ResolverControlOutcome::Interrupted)
        {
            return Err("dependency resolution interrupted".into());
        }
        if !output.status.success() {
            return Err(format!(
                "resolver workload failed ({}): {}",
                output.status,
                String::from_utf8_lossy(&output.stderr)
            ));
        }
        output.write_result.map_err(|e| e.to_string())?;
        if output.stdout.len() > LIMIT {
            return Err("resolver result exceeds 1 MiB".into());
        }
        // The pipe is retained throughout native retirement. No sandbox pathname
        // is opened here, and workload JSON never establishes cleanup success.
        let response: super::workload::Response = serde_json::from_slice(&output.stdout)
            .map_err(|e| format!("invalid resolver result: {e}"))?;
        if response.version != VERSION {
            return Err("incompatible resolver result version".into());
        }
        let value = response.result?;
        self.validate(&request.operation, &value)?;
        self.context = response.context;
        Ok(value)
    }

    fn validate(&self, operation: &Operation, value: &serde_json::Value) -> Result<(), String> {
        let Some(storage) = &self.storage else {
            return Ok(());
        };
        let inside = |path: &Path| -> Result<(), String> {
            let canonical = path
                .canonicalize()
                .map_err(|e| format!("invalid managed path {}: {e}", path.display()))?;
            if !path.is_absolute() || !canonical.starts_with(&storage.payload) {
                return Err(format!(
                    "managed artifact is outside resolver storage: {}",
                    path.display()
                ));
            }
            Ok(())
        };
        let python = |managed: super::ManagedPython| -> Result<(), String> {
            inside(managed.python())?;
            let native = managed
                .native()
                .ok_or("resolver omitted Python embedding configuration")?;
            if Path::new(&native.embedding.python) != managed.python() {
                return Err("resolver changed Python executable identity after inspection".into());
            }
            for path in [
                &native.embedding.python,
                &native.embedding.libpython,
                &native.prefix,
                &native.exec_prefix,
                &native.base_prefix,
                &native.base_exec_prefix,
            ] {
                inside(Path::new(path))?;
            }
            let home = if native.base_prefix == native.base_exec_prefix {
                native.base_prefix.clone()
            } else {
                format!("{}:{}", native.base_prefix, native.base_exec_prefix)
            };
            if native.embedding.python_home != home {
                return Err("resolver returned inconsistent Python home".into());
            }
            Ok(())
        };
        match operation {
            Operation::Python { requirements, .. } => {
                let managed: super::ManagedPython =
                    serde_json::from_value(value.clone()).map_err(|e| e.to_string())?;
                if managed.requirements() != &requirements.clone().normalized() {
                    return Err("resolver changed the accepted Python manifest".into());
                }
                python(managed)?;
            }
            Operation::R { requirements } => {
                let managed: super::ManagedR =
                    serde_json::from_value(value.clone()).map_err(|e| e.to_string())?;
                inside(managed.library())?;
                for path in managed.library_paths() {
                    inside(&path)?;
                }
                if managed.requirements() != requirements {
                    return Err("resolver changed the accepted R manifest".into());
                }
                if managed.extension_directory()
                    != Some(storage.payload.join("extensions").as_path())
                {
                    return Err("resolver changed managed extension storage".into());
                }
            }
            Operation::Discover => {
                let discovery: Discovery =
                    serde_json::from_value(value.clone()).map_err(|e| e.to_string())?;
                if let Some(managed) = discovery.python {
                    python(managed)?;
                }
            }
            _ => (),
        }
        Ok(())
    }

    pub(crate) fn release(self) -> Result<(), String> {
        self.storage
            .map_or(Ok(()), super::storage::Storage::release)
    }

    pub(crate) fn quarantine(self) -> Result<(), String> {
        self.storage
            .map_or(Ok(()), super::storage::Storage::quarantine)
    }
}

pub(crate) fn run() -> Result<(), String> {
    crate::resolver::preparation::run()
}
