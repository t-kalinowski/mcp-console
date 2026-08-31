#[cfg(target_os = "macos")]
impl SandboxedCommand {
    pub(crate) fn new(program: &OsStr) -> Result<Self, String> {
        let (command, temporary_directory) = platform::sandboxed_command()?;
        let temporary_directory_path = temporary_directory.path().as_os_str().to_os_string();
        let mut sandboxed = Self {
            command,
            temporary_directory,
            separate_process_group: false,
        };
        sandboxed
            .env("TMPDIR", temporary_directory_path)
            .arg(program);
        Ok(sandboxed)
    }

    pub(crate) fn arg(&mut self, argument: impl AsRef<OsStr>) -> &mut Self {
        self.command.arg(argument);
        self
    }

    pub(crate) fn args<I, S>(&mut self, arguments: I) -> &mut Self
    where
        I: IntoIterator<Item = S>,
        S: AsRef<OsStr>,
    {
        for argument in arguments {
            self.arg(argument);
        }
        self
    }

    /// Adds an environment variable inherited by the sandboxed program.
    ///
    /// macOS filters `DYLD_*` variables when launching `sandbox-exec`; this
    /// wrapper intentionally does not restore them inside the sandbox.
    /// `TMPDIR` is reserved and reset to the private directory when spawning.
    pub(crate) fn env(&mut self, key: impl AsRef<OsStr>, value: impl AsRef<OsStr>) -> &mut Self {
        self.command.env(key, value);
        self
    }

    pub(crate) fn env_remove(&mut self, key: impl AsRef<OsStr>) -> &mut Self {
        self.command.env_remove(key);
        self
    }

    pub(crate) fn stdin(&mut self, configuration: Stdio) -> &mut Self {
        self.command.stdin(configuration);
        self
    }

    pub(crate) fn stdout(&mut self, configuration: Stdio) -> &mut Self {
        self.command.stdout(configuration);
        self
    }

    pub(crate) fn stderr(&mut self, configuration: Stdio) -> &mut Self {
        self.command.stderr(configuration);
        self
    }

    /// Isolates a background sandbox command for bounded forced termination.
    pub(crate) fn new_process_group(&mut self) -> &mut Self {
        self.command.process_group(0);
        self.separate_process_group = true;
        self
    }

    /// Prevents descriptors other than fd 0, 1, and 2 from crossing exec.
    ///
    /// The caller may be multithreaded, so the descriptor scan runs in the
    /// forked child instead of relying on a parent-side snapshot.
    pub(crate) fn inherit_only_standard_streams(&mut self) -> Result<&mut Self, String> {
        file_descriptors::close_unlisted_from_multithreaded_parent(&mut self.command)?;
        Ok(self)
    }

    /// Spawns the sandboxed program, starts host-side descendant observation,
    /// commits an independent host crash manager, and transfers the private
    /// temporary-directory guard to the returned child.
    ///
    /// Darwin cannot atomically attach either observer at spawn time. A process
    /// that detaches before the post-spawn root watch or a fork event is observed
    /// remains outside this command's guarantee. Abrupt owner failure is covered
    /// only after the crash manager reports readiness; the spawn-to-commit window
    /// remains an intentional limitation of this slice.
    pub(crate) fn spawn(mut self) -> Result<SandboxedChild, String> {
        self.command.env("TMPDIR", self.temporary_directory.path());
        let mut child = self
            .command
            .spawn()
            .map_err(|error| format!("failed to launch `{}`: {error}", platform::SANDBOX_EXEC))?;
        let observed_lifetime = match supervision::ObservedLifetime::start(child.id()) {
            Ok(lifetime) => lifetime,
            Err(error) => {
                let error =
                    stop_after_observation_failure(&mut child, self.separate_process_group, error);
                // Observation failed before ownership was established. Preserve
                // the directory because a process that escaped observation may
                // still be using it even after process-group fallback cleanup.
                std::mem::forget(self.temporary_directory);
                return Err(error);
            }
        };
        let crash_manager = match supervision::SandboxManager::start(
            child.id(),
            self.temporary_directory.path(),
            self.separate_process_group,
            CRASH_MANAGER_CLEANUP_TIMEOUT,
        ) {
            Ok(manager) => manager,
            Err(error) => {
                let mut child = SandboxedChild {
                    child,
                    observed_lifetime: Some(observed_lifetime),
                    crash_manager: None,
                    retirement: SandboxedChildRetirement::Active,
                    separate_process_group: self.separate_process_group,
                    temporary_directory: Some(self.temporary_directory),
                };
                let cleanup = child.force_stop().err();
                return Err(cleanup.map_or(error.clone(), |cleanup| {
                    append_retirement_error(Some(error), cleanup)
                }));
            }
        };
        Ok(SandboxedChild {
            child,
            observed_lifetime: Some(observed_lifetime),
            crash_manager: Some(crash_manager),
            retirement: SandboxedChildRetirement::Active,
            separate_process_group: self.separate_process_group,
            temporary_directory: Some(self.temporary_directory),
        })
    }

    /// Runs a standalone command and retires descendants observed from its root.
    ///
    /// Darwin cannot atomically attach a descendant observer at spawn time. A
    /// process that detaches before the post-spawn root watch or a fork event is
    /// observed remains outside this command's guarantee. Termination or failure
    /// of the launcher itself is intentionally outside this command's scope.
    pub(crate) fn status(mut self) -> Result<ExitCode, String> {
        self.command.env("TMPDIR", self.temporary_directory.path());
        supervision::status(self.command, self.temporary_directory)
    }
}

#[cfg(target_os = "macos")]
fn stop_after_observation_failure(
    child: &mut Child,
    separate_process_group: bool,
    mut error: String,
) -> String {
    if separate_process_group && let Err(group_error) = platform::kill_process_group(child.id()) {
        error = append_retirement_error(
            Some(error),
            format!(
                "failed to stop `{}` process group: {group_error}",
                platform::SANDBOX_EXEC
            ),
        );
    }
    supervision::stop_direct_child(child, error)
}
