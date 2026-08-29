use std::collections::{BTreeMap, BTreeSet};
use std::ffi::{CString, OsStr, OsString};
use std::fs;
use std::io::{Read, Write};
use std::os::fd::{AsRawFd as _, FromRawFd as _, OwnedFd, RawFd};
use std::os::unix::ffi::OsStrExt as _;
use std::os::unix::fs::PermissionsExt as _;
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt as _;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitCode, ExitStatus, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{Value, json};

use super::protocol::{
    Control, FinalOutcome, expect_acknowledgment, expect_launch_accepted, expect_response_type,
    final_outcome, pin, validate_capabilities,
};
use super::temporary_directory::PrivateDirectory;

const PRIVATE_DESCRIPTOR_MINIMUM: RawFd = 64;
const RUNNER_NAME: &str = "mcp-console-sandbox";
const TEST_RUNNER_ENV: &str = "MCP_CONSOLE_TEST_SANDBOX_RUNNER";
const FORCE_TIMEOUT_MS: u64 = 2_000;
const FORCE_TIMEOUT: Duration = Duration::from_millis(FORCE_TIMEOUT_MS);
// Protocol v1 may spend two force-plus-confirmation windows, followed by one
// root-reap allowance after control EOF. Keep one second of scheduling margin.
const RUNNER_ALLOWANCE_MS: u64 = 1_000;
const DEADLINE_MARGIN_MS: u64 = 1_000;
const RETIREMENT_TIMEOUT_MS: u64 =
    2 * (FORCE_TIMEOUT_MS + RUNNER_ALLOWANCE_MS) + DEADLINE_MARGIN_MS;
const RUNNER_EXIT_TIMEOUT: Duration =
    Duration::from_millis(RETIREMENT_TIMEOUT_MS + RUNNER_ALLOWANCE_MS);
const POLL_INTERVAL: Duration = Duration::from_millis(5);

#[derive(Clone, Copy)]
pub(super) enum RuntimeMode {
    Command,
    Service,
}

impl RuntimeMode {
    fn as_str(self) -> &'static str {
        match self {
            Self::Command => "command",
            Self::Service => "service",
        }
    }
}

#[derive(Clone, Copy)]
enum StreamMode {
    Inherited,
    Piped,
}

#[derive(Clone, Copy)]
enum RunnerPhase {
    Running,
    RootExited,
    Retired,
}

pub(crate) struct SandboxedCommand {
    mode: RuntimeMode,
    program: OsString,
    arguments: Vec<OsString>,
    environment: BTreeMap<OsString, OsString>,
    stdin: StreamMode,
    stdout: StreamMode,
    stderr: StreamMode,
    inherited_signal_mask: Option<libc::sigset_t>,
    target_temporary_directory: PrivateDirectory,
}

#[must_use = "retain the sandboxed child until it is explicitly retired"]
pub(crate) struct SandboxedChild {
    runner: Child,
    control: Control,
    stdin: Option<SandboxedStdin>,
    stdout: Option<SandboxedOutput>,
    target_temporary_directory: PrivateDirectory,
    runner_state_directory: PrivateDirectory,
    final_outcome: Option<FinalOutcome>,
    runner_status: Option<ExitStatus>,
    root_process_id: Option<libc::pid_t>,
    launch_requested: bool,
    root_exited: bool,
    completed: bool,
}

struct PreparedStreams {
    child_endpoints: Vec<OwnedFd>,
    stdin: Option<SandboxedStdin>,
    stdout: Option<SandboxedOutput>,
    specifications: Value,
}

pub(crate) struct SandboxedStdin(fs::File);

impl Write for SandboxedStdin {
    fn write(&mut self, buffer: &[u8]) -> std::io::Result<usize> {
        self.0.write(buffer)
    }

    fn flush(&mut self) -> std::io::Result<()> {
        self.0.flush()
    }
}

pub(crate) struct SandboxedOutput(fs::File);

impl Read for SandboxedOutput {
    fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
        self.0.read(buffer)
    }
}

impl SandboxedCommand {
    pub(crate) fn service(program: &OsStr) -> Result<Self, String> {
        Self::new(RuntimeMode::Service, program)
    }

    pub(super) fn command(program: &OsStr) -> Result<Self, String> {
        Self::new(RuntimeMode::Command, program)
    }

    fn new(mode: RuntimeMode, program: &OsStr) -> Result<Self, String> {
        Ok(Self {
            mode,
            program: program.to_os_string(),
            arguments: Vec::new(),
            environment: std::env::vars_os().collect(),
            stdin: StreamMode::Inherited,
            stdout: StreamMode::Inherited,
            stderr: StreamMode::Inherited,
            inherited_signal_mask: None,
            target_temporary_directory: PrivateDirectory::new("tmp")?,
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
        self.environment
            .insert(key.as_ref().to_os_string(), value.as_ref().to_os_string());
        self
    }

    pub(crate) fn env_remove(&mut self, key: impl AsRef<OsStr>) -> &mut Self {
        self.environment.remove(key.as_ref());
        self
    }

    pub(crate) fn stdin_piped(&mut self) -> &mut Self {
        self.stdin = StreamMode::Piped;
        self
    }

    pub(crate) fn stdin_inherited(&mut self) -> &mut Self {
        self.stdin = StreamMode::Inherited;
        self
    }

    pub(crate) fn stdout_piped(&mut self) -> &mut Self {
        self.stdout = StreamMode::Piped;
        self
    }

    pub(crate) fn stdout_inherited(&mut self) -> &mut Self {
        self.stdout = StreamMode::Inherited;
        self
    }

    pub(crate) fn stderr_inherited(&mut self) -> &mut Self {
        self.stderr = StreamMode::Inherited;
        self
    }

    #[cfg(target_os = "macos")]
    pub(super) fn restore_signal_mask(&mut self, mask: libc::sigset_t) -> &mut Self {
        self.inherited_signal_mask = Some(mask);
        self
    }

    pub(crate) fn spawn(mut self) -> Result<SandboxedChild, String> {
        self.environment.insert(
            OsString::from("TMPDIR"),
            self.target_temporary_directory
                .path()
                .as_os_str()
                .to_os_string(),
        );
        let working_directory = std::env::current_dir()
            .map_err(|error| format!("failed to resolve the sandbox working directory: {error}"))?;
        let program = resolve_program(&self.program, &self.environment, &working_directory)?;
        let arguments = self.arguments.clone();
        let runner_path = runner_path()?;
        let pin = pin()?;
        let runner_state_directory = PrivateDirectory::new("runner")?;
        let runner_stdio = private_runner_stdio(runner_state_directory.path())?;
        let runner_stdin = runner_stdio
            .try_clone()
            .map_err(|error| format!("failed to prepare private runner input: {error}"))?;
        let runner_stdout = runner_stdio
            .try_clone()
            .map_err(|error| format!("failed to prepare private runner output: {error}"))?;
        let prepared_streams = PreparedStreams::new(self.stdin, self.stdout, self.stderr)?;
        let (control, child_control_source) = UnixStream::pair()
            .map_err(|error| format!("failed to create private sandbox control: {error}"))?;
        let child_control = duplicate_private_descriptor(child_control_source.as_raw_fd())?;
        drop(child_control_source);
        let child_control_descriptor = child_control.as_raw_fd();
        let endpoint_descriptors = prepared_streams
            .child_endpoints
            .iter()
            .map(|endpoint| endpoint.as_raw_fd())
            .collect::<Vec<_>>();

        let mut command = Command::new(&runner_path);
        command
            .arg("--state-dir")
            .arg(runner_state_directory.path())
            .arg("--cleanup-dir")
            .arg(self.target_temporary_directory.path())
            .arg("--control-fd")
            .arg(child_control_descriptor.to_string());
        for descriptor in &endpoint_descriptors {
            command.arg("--stream-fd").arg(descriptor.to_string());
        }
        command.arg("--").arg(&program).args(&arguments);
        command
            .env_clear()
            .stdin(Stdio::from(runner_stdin))
            .stdout(Stdio::from(runner_stdout))
            .stderr(Stdio::from(runner_stdio))
            .process_group(0);
        for (key, value) in &self.environment {
            if !is_loader_variable(key) && key != OsStr::new(TEST_RUNNER_ENV) {
                command.env(key, value);
            }
        }
        let descriptor_limit = descriptor_limit()?;
        let inherited_descriptors = endpoint_descriptors
            .iter()
            .copied()
            .chain([child_control_descriptor])
            .collect::<BTreeSet<_>>();
        let inherited_signal_mask = self.inherited_signal_mask;
        unsafe {
            command.pre_exec(move || {
                for descriptor in (libc::STDERR_FILENO + 1)..descriptor_limit {
                    configure_descriptor(descriptor, inherited_descriptors.contains(&descriptor))?;
                }
                if let Some(mask) = inherited_signal_mask {
                    let result =
                        libc::pthread_sigmask(libc::SIG_SETMASK, &mask, std::ptr::null_mut());
                    if result != 0 {
                        return Err(std::io::Error::from_raw_os_error(result));
                    }
                }
                Ok(())
            });
        }

        let control = Control::new(control, pin.protocol_version)?;
        let runner = command
            .spawn()
            .map_err(|error| format!("failed to start the private sandbox runner: {error}"))?;
        drop(child_control);
        drop(prepared_streams.child_endpoints);
        let mut child = SandboxedChild {
            runner,
            control,
            stdin: prepared_streams.stdin,
            stdout: prepared_streams.stdout,
            target_temporary_directory: self.target_temporary_directory,
            runner_state_directory,
            final_outcome: None,
            runner_status: None,
            root_process_id: None,
            launch_requested: false,
            root_exited: false,
            completed: false,
        };

        let result = (|| {
            let discovery = child.control.discover()?;
            validate_capabilities(&discovery, &pin)?;
            let launch = launch_request(
                self.mode,
                &working_directory,
                child.target_temporary_directory.path(),
                prepared_streams.specifications,
            )?;
            child.launch_requested = true;
            let response = child.control.launch(launch)?;
            expect_launch_accepted(&response)?;
            child.root_process_id = response
                .get("root_process_id")
                .and_then(Value::as_u64)
                .map(libc::pid_t::try_from)
                .transpose()
                .map_err(|_| "private sandbox returned an invalid target process identifier")?;
            #[cfg(target_os = "macos")]
            if !matches!(child.root_process_id, Some(process_id) if process_id > 0) {
                return Err(
                    "private sandbox launch omitted the target process identifier".to_string(),
                );
            }
            Ok(())
        })();
        if let Err(error) = result {
            let cleanup_error = child.abort_after_startup_failure();
            return Err(match cleanup_error {
                Ok(()) => error,
                Err(cleanup_error) => {
                    format!(
                        "{error}; additionally failed to retire sandbox startup: {cleanup_error}"
                    )
                }
            });
        }
        Ok(child)
    }
}

impl PreparedStreams {
    fn new(stdin: StreamMode, stdout: StreamMode, stderr: StreamMode) -> Result<Self, String> {
        let (stdin_endpoint, stdin_parent, stdin_specification) =
            prepare_input(stdin, libc::STDIN_FILENO)?;
        let (stdout_endpoint, stdout_parent, stdout_specification) =
            prepare_output(stdout, libc::STDOUT_FILENO)?;
        let (stderr_endpoint, _, stderr_specification) =
            prepare_output(stderr, libc::STDERR_FILENO)?;
        Ok(Self {
            child_endpoints: [stdin_endpoint, stdout_endpoint, stderr_endpoint]
                .into_iter()
                .flatten()
                .collect(),
            stdin: stdin_parent,
            stdout: stdout_parent,
            specifications: json!({
                "stdin": stdin_specification,
                "stdout": stdout_specification,
                "stderr": stderr_specification,
            }),
        })
    }
}

impl SandboxedChild {
    #[cfg(target_os = "macos")]
    pub(super) fn root_process_id(&self) -> libc::pid_t {
        self.root_process_id
            .expect("the macOS runner should report a target process identifier")
    }

    pub(crate) fn take_stdin(&mut self) -> Option<SandboxedStdin> {
        self.stdin.take()
    }

    pub(crate) fn take_stdout(&mut self) -> Option<SandboxedOutput> {
        self.stdout.take()
    }

    pub(crate) fn wait_timeout_without_reaping(
        &mut self,
        timeout: Duration,
    ) -> Result<bool, String> {
        match self.wait_timeout_open(timeout) {
            Ok(exited) => Ok(exited),
            Err(error) => Err(self.fail_active_operation(error)),
        }
    }

    fn wait_timeout_open(&mut self, timeout: Duration) -> Result<bool, String> {
        if self.final_outcome.is_some() {
            return Ok(true);
        }
        let deadline = Instant::now() + timeout;
        loop {
            if let Some(status) = self.observe_runner_exit()? {
                return Err(format!(
                    "private sandbox runner exited before target retirement with {status}"
                ));
            }
            if timeout.is_zero() {
                return Ok(false);
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Ok(false);
            }
            let Some(response) = self.control.status(remaining.min(Duration::from_secs(1)))? else {
                return Ok(false);
            };
            match runner_phase(&response)? {
                RunnerPhase::RootExited | RunnerPhase::Retired => {
                    self.root_exited = true;
                    return Ok(true);
                }
                RunnerPhase::Running => {}
            }
            thread::sleep(POLL_INTERVAL.min(deadline.saturating_duration_since(Instant::now())));
        }
    }

    pub(crate) fn interrupt(&mut self) -> Result<(), String> {
        let result = self
            .control
            .interrupt()
            .and_then(|response| expect_acknowledgment(&response, "interrupt"));
        match result {
            Ok(()) => Ok(()),
            Err(error) => Err(self.fail_active_operation(error)),
        }
    }

    #[cfg(target_os = "macos")]
    pub(super) fn forward_signal(&mut self, signal: libc::c_int) -> Result<(), String> {
        if signal == libc::SIGINT {
            return self.interrupt();
        }
        let process_group = self.root_process_id();
        if unsafe { libc::kill(-process_group, signal) } == 0 {
            return Ok(());
        }
        let error = std::io::Error::last_os_error();
        if error.raw_os_error() == Some(libc::ESRCH) {
            Ok(())
        } else {
            Err(format!(
                "failed to relay signal {signal} to the sandbox process group: {error}"
            ))
        }
    }

    pub(super) fn finish_exit_code(&mut self) -> Result<ExitCode, String> {
        let outcome = self.finish()?;
        target_exit_code(&outcome)
    }

    pub(crate) fn force_stop(&mut self) -> Result<(), String> {
        if self.completed {
            return self
                .final_outcome
                .as_ref()
                .map_or(Ok(()), validate_final_outcome);
        }
        self.retire()
    }

    fn finish(&mut self) -> Result<FinalOutcome, String> {
        if !self.completed {
            self.retire()?;
        }
        self.final_outcome
            .clone()
            .ok_or_else(|| "private sandbox completed without a final outcome".to_string())
    }

    fn retire(&mut self) -> Result<(), String> {
        let operation = if self.final_outcome.is_none() {
            self.request_retirement()
        } else {
            Ok(())
        };
        self.control.close();
        let reap = self.reap_runner();
        let outcome = self
            .final_outcome
            .as_ref()
            .map_or(Ok(()), validate_final_outcome);
        let mut errors = [operation.err(), outcome.err(), reap.err()]
            .into_iter()
            .flatten()
            .collect::<Vec<_>>();
        if errors.is_empty() {
            if let Err(error) = self.target_temporary_directory.confirm_removed() {
                errors.push(error);
            }
            if let Err(error) = self.runner_state_directory.remove() {
                errors.push(error);
            }
        }
        if errors.is_empty() {
            self.completed = true;
            Ok(())
        } else {
            self.preserve_directories();
            if self.runner_status.is_some() {
                self.completed = true;
            }
            Err(errors.join("; additionally "))
        }
    }

    fn request_retirement(&mut self) -> Result<(), String> {
        if !self.root_exited {
            let status = self
                .control
                .status(Duration::from_secs(2))?
                .ok_or_else(|| "private sandbox status response timed out".to_string())?;
            match runner_phase(&status)? {
                RunnerPhase::Running => {
                    let response = self
                        .control
                        .terminate(0, FORCE_TIMEOUT.as_millis() as u64)?;
                    expect_acknowledgment(&response, "terminate")?;
                }
                RunnerPhase::RootExited | RunnerPhase::Retired => {
                    self.root_exited = true;
                }
            }
        }
        self.final_outcome = Some(final_outcome(self.control.wait(RETIREMENT_TIMEOUT_MS)?)?);
        Ok(())
    }

    fn abort_after_startup_failure(&mut self) -> Result<(), String> {
        self.control.close();
        let reap = self.reap_runner();
        let cleanup = if !self.launch_requested && self.runner_status.is_some() {
            self.target_temporary_directory
                .remove()
                .and_then(|()| self.runner_state_directory.remove())
        } else if reap.is_ok() {
            self.cleanup_without_final_outcome()
        } else {
            Ok(())
        };
        let errors = [reap.err(), cleanup.err()]
            .into_iter()
            .flatten()
            .collect::<Vec<_>>();
        if errors.is_empty() {
            self.completed = true;
            Ok(())
        } else {
            self.preserve_directories();
            if self.runner_status.is_some() {
                self.completed = true;
            }
            Err(errors.join("; additionally "))
        }
    }

    fn fail_active_operation(&mut self, error: String) -> String {
        self.control.close();
        let reap = self.reap_runner();
        let cleanup = if reap.is_ok() {
            self.cleanup_without_final_outcome()
        } else {
            Ok(())
        };
        let mut errors = vec![error];
        errors.extend([reap.err(), cleanup.err()].into_iter().flatten());
        if errors.len() == 1 {
            self.completed = true;
        } else {
            self.preserve_directories();
            if self.runner_status.is_some() {
                self.completed = true;
            }
        }
        errors.join("; additionally ")
    }

    fn cleanup_without_final_outcome(&mut self) -> Result<(), String> {
        if self.launch_requested {
            self.target_temporary_directory.confirm_removed()?;
        } else {
            self.target_temporary_directory.remove()?;
        }
        self.runner_state_directory.remove()
    }

    fn reap_runner(&mut self) -> Result<(), String> {
        let mut errors = Vec::new();
        if self.runner_status.is_none() {
            let deadline = Instant::now() + RUNNER_EXIT_TIMEOUT;
            loop {
                match self.runner.try_wait() {
                    Ok(Some(status)) => {
                        self.runner_status = Some(status);
                        break;
                    }
                    Ok(None) => {}
                    Err(error) => {
                        errors.push(format!(
                            "failed to observe the private sandbox runner: {error}"
                        ));
                        break;
                    }
                }
                if Instant::now() >= deadline {
                    errors.push(
                        "private sandbox runner did not exit after control closure".to_string(),
                    );
                    break;
                }
                thread::sleep(POLL_INTERVAL);
            }
        }

        if self.runner_status.is_none() {
            if let Err(error) = self.runner.kill()
                && error.raw_os_error() != Some(libc::ESRCH)
            {
                errors.push(format!(
                    "failed to stop an unresponsive private sandbox runner: {error}"
                ));
            }
            match self.runner.wait() {
                Ok(status) => self.runner_status = Some(status),
                Err(error) => errors.push(format!(
                    "failed to reap an unresponsive private sandbox runner: {error}"
                )),
            }
        }
        if let Some(status) = self.runner_status
            && !status.success()
        {
            errors.push(format!("private sandbox runner exited with {status}"));
        }
        if errors.is_empty() {
            Ok(())
        } else {
            Err(errors.join("; additionally "))
        }
    }

    fn observe_runner_exit(&mut self) -> Result<Option<ExitStatus>, String> {
        if let Some(status) = self.runner_status {
            return Ok(Some(status));
        }
        let status = self
            .runner
            .try_wait()
            .map_err(|error| format!("failed to observe the private sandbox runner: {error}"))?;
        if let Some(status) = status {
            self.runner_status = Some(status);
        }
        Ok(status)
    }

    fn preserve_directories(&mut self) {
        self.target_temporary_directory.preserve();
        self.runner_state_directory.preserve();
    }
}

impl Drop for SandboxedChild {
    fn drop(&mut self) {
        if !self.completed && self.force_stop().is_err() {
            self.preserve_directories();
        }
    }
}

fn runner_phase(response: &Value) -> Result<RunnerPhase, String> {
    expect_response_type(response, "status")?;
    match response["status"]["phase"].as_str() {
        Some("running") => Ok(RunnerPhase::Running),
        Some("root_exited") => Ok(RunnerPhase::RootExited),
        Some("retired") => Ok(RunnerPhase::Retired),
        Some("failed") => Err("private sandbox runner entered a failed state".to_string()),
        Some(phase) => Err(format!(
            "private sandbox runner returned unexpected phase {phase}"
        )),
        None => Err("private sandbox status omitted its phase".to_string()),
    }
}

fn launch_request(
    mode: RuntimeMode,
    working_directory: &Path,
    target_temporary_directory: &Path,
    streams: Value,
) -> Result<Value, String> {
    let working_directory = working_directory
        .to_str()
        .ok_or_else(|| "sandbox working directory must be valid UTF-8".to_string())?;
    let target_temporary_directory = target_temporary_directory
        .to_str()
        .ok_or_else(|| "sandbox temporary directory must be valid UTF-8".to_string())?;
    let rules = vec![json!({
        "path": target_temporary_directory,
        "access": "write",
        "missing": "error",
    })];
    Ok(json!({
        "working_directory": working_directory,
        "policy_base_directory": working_directory,
        "filesystem": {
            "base": "host_read_only",
            "rules": rules,
        },
        "network": { "mode": "denied" },
        "streams": streams,
        "terminal": "preserve",
        "lifecycle": {
            "kind": mode.as_str(),
            "root_exit_grace_ms": 0,
            "terminate_grace_ms": 0,
            "force_timeout_ms": FORCE_TIMEOUT.as_millis() as u64,
        },
        "platform_extensions": {},
    }))
}

fn prepare_input(
    mode: StreamMode,
    inherited: RawFd,
) -> Result<(Option<OwnedFd>, Option<SandboxedStdin>, Value), String> {
    match mode {
        StreamMode::Inherited => inherited_stream(inherited),
        StreamMode::Piped => {
            let (child_source, parent) = pipe()?;
            let child = duplicate_private_descriptor(child_source.as_raw_fd())?;
            let handle = child.as_raw_fd();
            Ok((
                Some(child),
                Some(SandboxedStdin(parent.into())),
                json!({ "mode": "passed_handle", "handle": handle }),
            ))
        }
    }
}

fn prepare_output(
    mode: StreamMode,
    inherited: RawFd,
) -> Result<(Option<OwnedFd>, Option<SandboxedOutput>, Value), String> {
    match mode {
        StreamMode::Inherited => inherited_stream(inherited),
        StreamMode::Piped => {
            let (parent, child_source) = pipe()?;
            let child = duplicate_private_descriptor(child_source.as_raw_fd())?;
            let handle = child.as_raw_fd();
            Ok((
                Some(child),
                Some(SandboxedOutput(parent.into())),
                json!({ "mode": "passed_handle", "handle": handle }),
            ))
        }
    }
}

fn inherited_stream<T>(inherited: RawFd) -> Result<(Option<OwnedFd>, Option<T>, Value), String> {
    let descriptor = duplicate_private_descriptor(inherited).map_err(|error| {
        format!("failed to duplicate inherited sandbox stream {inherited}: {error}")
    })?;
    let handle = descriptor.as_raw_fd();
    Ok((
        Some(descriptor),
        None,
        json!({ "mode": "passed_handle", "handle": handle }),
    ))
}

fn pipe() -> Result<(OwnedFd, OwnedFd), String> {
    let mut descriptors = [-1; 2];
    if unsafe { libc::pipe(descriptors.as_mut_ptr()) } != 0 {
        return Err(format!(
            "failed to create a sandbox stream: {}",
            std::io::Error::last_os_error()
        ));
    }
    let read = unsafe { OwnedFd::from_raw_fd(descriptors[0]) };
    let write = unsafe { OwnedFd::from_raw_fd(descriptors[1]) };
    configure_descriptor(read.as_raw_fd(), false)
        .and_then(|()| configure_descriptor(write.as_raw_fd(), false))
        .map_err(|error| format!("failed to configure a sandbox stream: {error}"))?;
    Ok((read, write))
}

fn runner_path() -> Result<PathBuf, String> {
    if cfg!(debug_assertions)
        && let Some(path) = std::env::var_os(TEST_RUNNER_ENV)
    {
        let path = PathBuf::from(path);
        if !path.is_absolute() {
            return Err(format!("{TEST_RUNNER_ENV} must be an absolute path"));
        }
        return executable_file(path);
    }
    let executable = std::env::current_exe()
        .map_err(|error| format!("failed to locate the MCP Console installation: {error}"))?;
    let executable = executable
        .canonicalize()
        .map_err(|error| format!("failed to resolve the MCP Console installation: {error}"))?;
    let bin_directory = executable
        .parent()
        .ok_or_else(|| "failed to locate the MCP Console installation directory".to_string())?;
    let libexec = if bin_directory.file_name() == Some(OsStr::new("bin")) {
        bin_directory
            .parent()
            .ok_or_else(|| "failed to locate the MCP Console installation prefix".to_string())?
            .join("libexec")
    } else {
        bin_directory.join("libexec")
    };
    executable_file(libexec.join(RUNNER_NAME))
}

fn executable_file(path: PathBuf) -> Result<PathBuf, String> {
    let metadata = path
        .metadata()
        .map_err(|_| "the private sandbox runner is unavailable".to_string())?;
    if !metadata.is_file() || metadata.permissions().mode() & 0o111 == 0 {
        return Err("the private sandbox runner is unavailable".to_string());
    }
    Ok(path)
}

fn private_runner_stdio(state_directory: &Path) -> Result<fs::File, String> {
    let path = state_directory.join("stdio");
    let file = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .open(&path)
        .map_err(|error| format!("failed to prepare private runner streams: {error}"))?;
    file.set_permissions(fs::Permissions::from_mode(0o600))
        .and_then(|()| fs::remove_file(&path))
        .map_err(|error| format!("failed to secure private runner streams: {error}"))?;
    Ok(file)
}

fn resolve_program(
    program: &OsStr,
    environment: &BTreeMap<OsString, OsString>,
    working_directory: &Path,
) -> Result<PathBuf, String> {
    let path = Path::new(program);
    if path.is_absolute() {
        return Ok(path.to_path_buf());
    }
    if program.as_bytes().contains(&b'/') {
        return Ok(working_directory.join(path));
    }
    let search = environment
        .get(OsStr::new("PATH"))
        .cloned()
        .unwrap_or_else(|| OsString::from("/usr/bin:/bin"));
    for directory in std::env::split_paths(&search) {
        let candidate = working_directory.join(directory).join(path);
        if is_executable_file(&candidate) {
            return Ok(candidate);
        }
    }
    Err(format!(
        "failed to resolve sandbox executable `{}` on PATH",
        program.to_string_lossy()
    ))
}

fn is_executable_file(path: &Path) -> bool {
    if !path.metadata().is_ok_and(|metadata| metadata.is_file()) {
        return false;
    }
    let Ok(path) = CString::new(path.as_os_str().as_bytes()) else {
        return false;
    };
    (unsafe { libc::access(path.as_ptr(), libc::X_OK) }) == 0
}

fn validate_final_outcome(outcome: &FinalOutcome) -> Result<(), String> {
    let mut errors = Vec::new();
    match &outcome.target {
        Some(target) => {
            let valid = match target.kind.as_str() {
                "exited" => {
                    target.code.is_some() && target.signal.is_none() && target.error.is_none()
                }
                "signaled" => {
                    target.code.is_none()
                        && target.signal.is_some_and(|signal| signal > 0)
                        && target.error.is_none()
                }
                "unknown" => {
                    target.code.is_none()
                        && target.signal.is_none()
                        && target.error.as_ref().is_some_and(|error| !error.is_empty())
                }
                _ => false,
            };
            if !valid {
                errors.push("private sandbox returned an invalid target outcome".to_string());
            }
        }
        None if outcome.infrastructure.error.is_none() => {
            errors.push("private sandbox final outcome omitted its target result".to_string());
        }
        None => {}
    }
    if !outcome.retirement.complete {
        errors.push(
            outcome
                .retirement
                .error
                .clone()
                .unwrap_or_else(|| "sandbox target tree retirement was incomplete".to_string()),
        );
    }
    if let Some(error) = &outcome.infrastructure.error {
        errors.push(format!("sandbox infrastructure failed: {error}"));
    }
    if let Some(error) = &outcome.infrastructure.cleanup_error {
        errors.push(format!("sandbox cleanup failed: {error}"));
    }
    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors.join("; additionally "))
    }
}

fn target_exit_code(outcome: &FinalOutcome) -> Result<ExitCode, String> {
    validate_final_outcome(outcome)?;
    let target = outcome
        .target
        .as_ref()
        .ok_or_else(|| "sandbox target outcome is unavailable".to_string())?;
    if let Some(error) = &target.error {
        return Err(format!("sandbox target outcome failed: {error}"));
    }
    match (target.kind.as_str(), target.code, target.signal) {
        ("exited", Some(code), None) => exit_code(code),
        ("signaled", None, Some(signal)) if signal > 0 => signal
            .checked_add(128)
            .map(i64::from)
            .ok_or_else(|| "private sandbox returned an invalid target outcome".to_string())
            .and_then(exit_code),
        ("exited" | "signaled", _, _) => {
            Err("private sandbox returned an invalid target outcome".to_string())
        }
        (kind, _, _) => Err(format!(
            "sandbox target returned an unsupported outcome {kind}"
        )),
    }
}

fn exit_code(value: i64) -> Result<ExitCode, String> {
    u8::try_from(value)
        .map(ExitCode::from)
        .map_err(|_| format!("sandbox target returned invalid exit status {value}"))
}

fn is_loader_variable(name: &OsStr) -> bool {
    let bytes = name.as_bytes();
    bytes.starts_with(b"LD_") || bytes.starts_with(b"DYLD_")
}

fn duplicate_private_descriptor(source: RawFd) -> Result<OwnedFd, String> {
    loop {
        let descriptor =
            unsafe { libc::fcntl(source, libc::F_DUPFD_CLOEXEC, PRIVATE_DESCRIPTOR_MINIMUM) };
        if descriptor >= 0 {
            return Ok(unsafe { OwnedFd::from_raw_fd(descriptor) });
        }
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::Interrupted {
            return Err(format!(
                "failed to reserve a private sandbox descriptor: {error}"
            ));
        }
    }
}

fn descriptor_limit() -> Result<RawFd, String> {
    let table_size = unsafe { libc::getdtablesize() };
    if table_size <= 0 {
        return Err(format!(
            "failed to read the launcher file-descriptor limit: {}",
            std::io::Error::last_os_error()
        ));
    }
    Ok(open_descriptors()?
        .into_iter()
        .max()
        .map_or(table_size, |descriptor| {
            table_size.max(descriptor.saturating_add(1))
        }))
}

fn open_descriptors() -> Result<Vec<RawFd>, String> {
    let mut capacity = 16;
    loop {
        let mut descriptors: Vec<libc::proc_fdinfo> = Vec::with_capacity(capacity);
        descriptors.resize_with(capacity, || unsafe { std::mem::zeroed() });
        unsafe { *libc::__error() = 0 };
        let size = unsafe {
            libc::proc_pidinfo(
                libc::getpid(),
                libc::PROC_PIDLISTFDS,
                0,
                descriptors.as_mut_ptr().cast(),
                std::mem::size_of_val(descriptors.as_slice()) as libc::c_int,
            )
        };
        if size == 0 {
            let error_code = unsafe { *libc::__error() };
            if error_code == 0 {
                return Ok(Vec::new());
            }
            if error_code == libc::EINTR {
                continue;
            }
            return Err(format!(
                "failed to list launcher file descriptors: {}",
                std::io::Error::from_raw_os_error(error_code)
            ));
        }
        if size < 0 || !(size as usize).is_multiple_of(std::mem::size_of::<libc::proc_fdinfo>()) {
            return Err(format!(
                "failed to list launcher file descriptors: proc_pidinfo returned {size} bytes"
            ));
        }
        let count = size as usize / std::mem::size_of::<libc::proc_fdinfo>();
        if count < capacity {
            descriptors.truncate(count);
            return Ok(descriptors
                .into_iter()
                .map(|descriptor| descriptor.proc_fd)
                .collect());
        }
        capacity = capacity.saturating_mul(2).max(count + 16);
    }
}

fn configure_descriptor(descriptor: RawFd, inherited: bool) -> std::io::Result<()> {
    if descriptor <= libc::STDERR_FILENO {
        return Ok(());
    }
    let flags = loop {
        let flags = unsafe { libc::fcntl(descriptor, libc::F_GETFD) };
        if flags >= 0 {
            break flags;
        }
        let error = std::io::Error::last_os_error();
        match error.raw_os_error() {
            Some(libc::EINTR) => continue,
            Some(libc::EBADF) => return Ok(()),
            _ => return Err(error),
        }
    };
    let desired = if inherited {
        flags & !libc::FD_CLOEXEC
    } else {
        flags | libc::FD_CLOEXEC
    };
    if desired == flags {
        return Ok(());
    }
    loop {
        if unsafe { libc::fcntl(descriptor, libc::F_SETFD, desired) } == 0 {
            return Ok(());
        }
        let error = std::io::Error::last_os_error();
        match error.raw_os_error() {
            Some(libc::EINTR) => continue,
            Some(libc::EBADF) => return Ok(()),
            _ => return Err(error),
        }
    }
}
