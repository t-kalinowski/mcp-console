use std::collections::BTreeMap;
use std::ffi::{CString, OsStr, OsString};
use std::fs;
use std::os::unix::ffi::OsStrExt as _;
use std::os::unix::fs::PermissionsExt as _;
use std::path::{Component, Path, PathBuf};
use std::process::ExitCode;
use std::sync::Arc;
use std::time::Duration;

use codex_sandbox_api::{
    CommandSpec, SandboxBackend, SandboxExitStatus, SandboxLifetime, SandboxPolicy, SandboxRequest,
    SandboxRuntime as ApiRuntime, SandboxRuntimeConfig, SandboxStdioMode,
};

use super::driver::Driver;
pub(crate) use super::driver::{SandboxedOutput, SandboxedStdin};

const EXPECTED_SANDBOX_API_VERSION: u32 = 2;
const DARWIN_DEFAULT_EXEC_PATH: &str = "/usr/bin:/bin";

#[derive(Clone)]
pub(crate) struct SandboxRuntime(Arc<RuntimeOwner>);

struct RuntimeOwner {
    api: Arc<ApiRuntime>,
    driver: Driver,
    _state_directory: PrivateDirectory,
}

struct PrivateDirectory {
    _directory: tempfile::TempDir,
    path: PathBuf,
}

#[derive(Clone)]
struct SandboxLease {
    runtime: SandboxRuntime,
    _launch_directory: Arc<PrivateDirectory>,
}

pub(crate) struct SandboxedCommand {
    runtime: SandboxRuntime,
    program: OsString,
    arguments: Vec<OsString>,
    environment: BTreeMap<OsString, Option<OsString>>,
    stdin: SandboxStdioMode,
    stdout: SandboxStdioMode,
    stderr: SandboxStdioMode,
    lifetime: SandboxLifetime,
    launch_directory: Arc<PrivateDirectory>,
}

#[must_use = "retain the sandboxed child until its process and streams are retired"]
pub(crate) struct SandboxedChild {
    process: SandboxedProcess,
    stdin: Option<SandboxedStdin>,
    stdout: Option<SandboxedOutput>,
    stderr: Option<SandboxedOutput>,
}

#[derive(Clone)]
pub(crate) struct SandboxedProcess {
    process: codex_sandbox_api::SandboxedProcess,
    lease: SandboxLease,
}

impl SandboxRuntime {
    pub(crate) fn new() -> Result<Self, String> {
        if codex_sandbox_api::SANDBOX_API_VERSION != EXPECTED_SANDBOX_API_VERSION {
            return Err(format!(
                "unsupported sandbox API version {}; expected {EXPECTED_SANDBOX_API_VERSION}",
                codex_sandbox_api::SANDBOX_API_VERSION
            ));
        }

        let state_directory = PrivateDirectory::new("mcp-console-sandbox-state-")?;
        let api = ApiRuntime::new(SandboxRuntimeConfig::new(&state_directory.path))
            .map_err(|error| format!("failed to initialize the sandbox runtime: {error}"))?;
        validate_capabilities(api.capabilities())?;
        let driver = Driver::new()?;
        Ok(Self(Arc::new(RuntimeOwner {
            api: Arc::new(api),
            driver,
            _state_directory: state_directory,
        })))
    }
}

impl PrivateDirectory {
    fn new(prefix: &str) -> Result<Self, String> {
        let directory = tempfile::Builder::new()
            .prefix(prefix)
            .tempdir()
            .map_err(|error| format!("failed to create private directory: {error}"))?;
        fs::set_permissions(directory.path(), fs::Permissions::from_mode(0o700)).map_err(
            |error| {
                format!(
                    "failed to make private directory `{}` user-private: {error}",
                    directory.path().display()
                )
            },
        )?;
        let path = directory.path().canonicalize().map_err(|error| {
            format!(
                "failed to resolve private directory `{}`: {error}",
                directory.path().display()
            )
        })?;
        Ok(Self {
            _directory: directory,
            path,
        })
    }
}

fn validate_capabilities(
    capabilities: codex_sandbox_api::SandboxCapabilities,
) -> Result<(), String> {
    let mut missing = Vec::new();
    if capabilities.backend != SandboxBackend::MacosSeatbelt {
        missing.push("macOS Seatbelt");
    }
    if !capabilities.denied_write_paths {
        missing.push("write restriction");
    }
    if !capabilities.network_denial {
        missing.push("network denial");
    }
    if !capabilities.terminal_isolation {
        missing.push("terminal isolation");
    }
    if !capabilities.process_tree_termination {
        missing.push("supervised process-tree termination");
    }
    if missing.is_empty() {
        Ok(())
    } else {
        Err(format!(
            "selected sandbox backend lacks required capabilities: {}",
            missing.join(", ")
        ))
    }
}

impl SandboxedCommand {
    pub(crate) fn new(runtime: &SandboxRuntime, program: &OsStr) -> Result<Self, String> {
        Ok(Self {
            runtime: runtime.clone(),
            program: program.to_os_string(),
            arguments: Vec::new(),
            environment: BTreeMap::new(),
            stdin: SandboxStdioMode::Inherit,
            stdout: SandboxStdioMode::Inherit,
            stderr: SandboxStdioMode::Inherit,
            lifetime: SandboxLifetime::BackendDefault,
            launch_directory: Arc::new(PrivateDirectory::new("mcp-console-tmp-")?),
        })
    }

    pub(crate) fn arg(&mut self, argument: impl AsRef<OsStr>) -> &mut Self {
        self.arguments.push(argument.as_ref().to_os_string());
        self
    }

    pub(crate) fn args<I, S>(&mut self, arguments: I) -> &mut Self
    where
        I: IntoIterator<Item = S>,
        S: AsRef<OsStr>,
    {
        self.arguments.extend(
            arguments
                .into_iter()
                .map(|argument| argument.as_ref().to_os_string()),
        );
        self
    }

    pub(crate) fn env(&mut self, key: impl AsRef<OsStr>, value: impl AsRef<OsStr>) -> &mut Self {
        self.environment.insert(
            key.as_ref().to_os_string(),
            Some(value.as_ref().to_os_string()),
        );
        self
    }

    pub(crate) fn env_remove(&mut self, key: impl AsRef<OsStr>) -> &mut Self {
        self.environment.insert(key.as_ref().to_os_string(), None);
        self
    }

    pub(crate) fn stdin(&mut self, mode: SandboxStdioMode) -> &mut Self {
        self.stdin = mode;
        self
    }

    pub(crate) fn stdout(&mut self, mode: SandboxStdioMode) -> &mut Self {
        self.stdout = mode;
        self
    }

    pub(crate) fn stderr(&mut self, mode: SandboxStdioMode) -> &mut Self {
        self.stderr = mode;
        self
    }

    pub(crate) fn supervised_process_tree(&mut self) -> &mut Self {
        self.lifetime = SandboxLifetime::SupervisedProcessTree;
        self
    }

    pub(crate) fn spawn(self) -> Result<SandboxedChild, String> {
        let Self {
            runtime,
            program,
            arguments,
            environment,
            stdin,
            stdout,
            stderr,
            lifetime,
            launch_directory,
        } = self;
        let cwd = std::env::current_dir()
            .map_err(|error| format!("failed to read the current working directory: {error}"))?;
        let environment = complete_environment(environment, &launch_directory.path);
        let program = resolve_executable(&program, &cwd, &environment)?;
        let command = CommandSpec::new(program, cwd, environment).args(arguments);
        let policy = SandboxPolicy::host_read_only()
            .read_write(&launch_directory.path)
            .network_denied()
            .terminal_inherited_or_created();
        let request = SandboxRequest::new(command, policy)
            .stdin(stdin)
            .stdout(stdout)
            .stderr(stderr)
            .lifetime(lifetime);
        let api = Arc::clone(&runtime.0.api);
        let mut child = runtime
            .0
            .driver
            .run(async move { api.spawn(request).await.map_err(|error| error.to_string()) })?;
        let lease = SandboxLease {
            runtime,
            _launch_directory: launch_directory,
        };
        let process = SandboxedProcess {
            process: child.process(),
            lease: lease.clone(),
        };
        let driver = lease.runtime.0.driver.clone();
        let stdin = child
            .take_stdin()
            .map(|stdin| SandboxedStdin::new(&driver, stdin, lease.clone()));
        let stdout = child
            .take_stdout()
            .map(|stdout| SandboxedOutput::new(&driver, stdout, lease.clone()));
        let stderr = child
            .take_stderr()
            .map(|stderr| SandboxedOutput::new(&driver, stderr, lease));
        Ok(SandboxedChild {
            process,
            stdin,
            stdout,
            stderr,
        })
    }
}

fn complete_environment(
    changes: BTreeMap<OsString, Option<OsString>>,
    temporary_directory: &Path,
) -> BTreeMap<OsString, OsString> {
    let mut environment = std::env::vars_os().collect::<BTreeMap<_, _>>();
    for (key, value) in changes {
        if let Some(value) = value {
            environment.insert(key, value);
        } else {
            environment.remove(&key);
        }
    }
    environment.retain(|key, _| !key.as_encoded_bytes().starts_with(b"DYLD_"));
    environment.insert(
        OsString::from("TMPDIR"),
        temporary_directory.as_os_str().to_os_string(),
    );
    environment
}

fn resolve_executable(
    program: &OsStr,
    cwd: &Path,
    environment: &BTreeMap<OsString, OsString>,
) -> Result<OsString, String> {
    let path = Path::new(program);
    if path.is_absolute() {
        return Ok(program.to_os_string());
    }
    if !is_bare_executable(path) {
        return Ok(cwd.join(path).into_os_string());
    }

    // Darwin's `execvp` uses `_PATH_DEFPATH` only when PATH is absent. A
    // present empty PATH remains a search of the current directory.
    let search_path = environment
        .get(OsStr::new("PATH"))
        .map(OsString::as_os_str)
        .unwrap_or_else(|| OsStr::new(DARWIN_DEFAULT_EXEC_PATH));
    for directory in std::env::split_paths(search_path) {
        let directory = if directory.is_absolute() {
            directory
        } else {
            cwd.join(directory)
        };
        let candidate = directory.join(path);
        if is_executable_file(&candidate) {
            return Ok(candidate.into_os_string());
        }
    }
    Err(format!(
        "failed to resolve executable `{}` through the child PATH",
        path.display()
    ))
}

fn is_bare_executable(path: &Path) -> bool {
    let mut components = path.components();
    matches!(components.next(), Some(Component::Normal(_))) && components.next().is_none()
}

fn is_executable_file(path: &Path) -> bool {
    let Ok(path_string) = CString::new(path.as_os_str().as_bytes()) else {
        return false;
    };
    fs::metadata(path).is_ok_and(|metadata| metadata.is_file())
        // SAFETY: `path_string` is a NUL-terminated copy of the candidate path.
        && unsafe { libc::access(path_string.as_ptr(), libc::X_OK) } == 0
}

impl SandboxedChild {
    pub(crate) fn take_stdin(&mut self) -> Option<SandboxedStdin> {
        self.stdin.take()
    }

    pub(crate) fn take_stdout(&mut self) -> Option<SandboxedOutput> {
        self.stdout.take()
    }

    #[allow(dead_code, reason = "available for sandboxed callers that pipe stderr")]
    pub(crate) fn take_stderr(&mut self) -> Option<SandboxedOutput> {
        self.stderr.take()
    }

    pub(crate) fn process(&self) -> SandboxedProcess {
        self.process.clone()
    }
}

impl SandboxedProcess {
    pub(crate) fn wait_root_timeout(&self, timeout: Duration) -> Result<bool, String> {
        let process = self.process.clone();
        self.lease.runtime.0.driver.run(async move {
            match tokio::time::timeout(timeout, process.wait_root()).await {
                Ok(result) => result.map(|_| true).map_err(|error| error.to_string()),
                Err(_) => Ok(false),
            }
        })
    }

    pub(crate) fn terminate(&self) -> Result<(), String> {
        self.process.terminate().map_err(|error| error.to_string())
    }

    pub(crate) fn retire(&self) -> Result<SandboxExitStatus, String> {
        let process = self.process.clone();
        self.lease
            .runtime
            .0
            .driver
            .run(async move { process.retire().await.map_err(|error| error.to_string()) })
    }
}

pub(super) fn run(command_line: &[OsString]) -> Result<ExitCode, String> {
    let (program, arguments) = command_line
        .split_first()
        .expect("sandbox command must include a program");
    let runtime = SandboxRuntime::new()?;
    let mut command = SandboxedCommand::new(&runtime, program)?;
    command
        .args(arguments)
        .stdin(SandboxStdioMode::Inherit)
        .stdout(SandboxStdioMode::Inherit)
        .stderr(SandboxStdioMode::Inherit);
    let child = command.spawn()?;
    let status = child.process().retire()?;
    Ok(exit_code(status))
}

fn exit_code(status: SandboxExitStatus) -> ExitCode {
    let code = status
        .code()
        .unwrap_or_else(|| 128 + status.signal().unwrap_or(1));
    ExitCode::from(code as u8)
}
