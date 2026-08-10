#[cfg(target_os = "macos")]
mod platform {
    use std::collections::BTreeMap;
    use std::io::{self, Write};
    use std::mem::MaybeUninit;
    use std::os::unix::process::CommandExt as _;
    use std::path::{Path, PathBuf};
    use std::process::{Child, ChildStdin, Command, ExitStatus, Stdio};
    use std::sync::mpsc::{self, Receiver, Sender};
    use std::thread;

    use serde::Serialize;

    const PYTHON_RESOLVER: &str = r#"
base::local({
  input <- base::paste(base::readLines(
    base::file("stdin", encoding = "UTF-8"),
    warn = FALSE
  ), collapse = "\n")
  input <- jsonlite::fromJSON(input)
  packages <- base::unique(c("numpy", input$packages))
  python_version <- input$python_version
  if (!base::length(python_version)) {
    python_version <- NULL
  }
  exclude_newer <- input$exclude_newer
  if (!base::length(exclude_newer)) {
    exclude_newer <- NULL
  }
  messages <- utils::capture.output(
    ignored_output <- utils::capture.output(
      python <- base::try(
        reticulate:::uv_get_or_create_env(
          packages = packages,
          python_version = python_version,
          exclude_newer = exclude_newer
        ),
        silent = TRUE
      ),
      type = "output"
    ),
    type = "message"
  )
  if (base::inherits(python, "try-error")) {
    python_selection <- base::grep(
      "^[[:space:]]*Python:",
      messages,
      value = TRUE
    )
    uv_error <- base::any(base::startsWith(messages, "uv error code: "))
    if (uv_error && base::length(python_selection)) {
      base::cat(
        base::sub(
          "^[[:space:]]*Python:[[:space:]]*",
          "",
          python_selection[[1L]]
        ),
        "\n",
        sep = ""
      )
    } else {
      error <- base::attr(python, "condition")
      base::writeLines(base::conditionMessage(error), con = base::stderr())
    }
    base::quit(save = "no", status = 1L, runLast = FALSE)
  }
  base::stopifnot(
    base::length(python) == 1L,
    !base::is.na(python),
    base::nzchar(python)
  )
  base::cat(python, "\n", sep = "")
})
"#;

    #[derive(Clone)]
    pub(crate) struct ManagedPython {
        python: PathBuf,
        requirements: crate::worker_protocol::PythonRequirementManifest,
    }

    #[derive(Clone)]
    pub(crate) struct ResolverStopHandle(Sender<ResolverEvent>);

    enum ResolverEvent {
        Cancel,
        Exited(io::Result<()>),
    }

    struct ResolverOutput {
        status: ExitStatus,
        write_result: io::Result<()>,
        stdout: Vec<u8>,
        stderr: Vec<u8>,
    }

    #[derive(Serialize)]
    struct ResolverInput<'a> {
        python: &'a str,
        packages: Vec<&'a str>,
        #[serde(skip_serializing_if = "Vec::is_empty")]
        python_version: Vec<&'a str>,
        #[serde(skip_serializing_if = "Option::is_none")]
        exclude_newer: Option<&'a str>,
    }

    impl ManagedPython {
        pub(crate) fn configure_worker(&self, command: &mut crate::sandbox::SandboxedCommand) {
            command.env("RETICULATE_PYTHON", "managed");
            command.env(
                "MCP_CONSOLE_MANAGED_PYTHON",
                serde_json::to_string(&self.requirements)
                    .expect("managed Python requirements should serialize as JSON"),
            );
        }

        pub(crate) fn python(&self) -> &Path {
            &self.python
        }

        pub(crate) fn requirements(&self) -> &crate::worker_protocol::PythonRequirementManifest {
            &self.requirements
        }
    }

    pub(crate) fn resolve_python(
        requirements: &[String],
        on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<Option<ManagedPython>, String> {
        if requirements.is_empty()
            && std::env::var_os("RETICULATE_PYTHON").is_some_and(|value| value != "managed")
        {
            return Ok(None);
        }
        let requirements = manifest_from_packages(requirements);
        let environment = uv_environment();
        resolve_python_manifest(requirements, environment, on_started).map(Some)
    }

    pub(crate) fn resolve_python_manifest(
        requirements: crate::worker_protocol::PythonRequirementManifest,
        environment: BTreeMap<String, String>,
        on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<ManagedPython, String> {
        let requirements = requirements.normalized();
        validate_environment(&environment)?;
        let rscript = std::env::var_os("R_HOME")
            .map(|r_home| PathBuf::from(r_home).join("bin/Rscript"))
            .unwrap_or_else(|| PathBuf::from("Rscript"));
        let mut command = Command::new(&rscript);
        command
            .args(["--vanilla", "-e", PYTHON_RESOLVER])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .process_group(0);
        for name in std::env::vars_os()
            .filter_map(|(name, _)| name.into_string().ok())
            .filter(|name| name.starts_with("UV_"))
        {
            command.env_remove(name);
        }
        command.envs(&environment).env_remove("UV_OFFLINE");
        // Managed resolution intentionally runs before the sandboxed worker starts
        // because reticulate and uv need normal host network and cache access.
        // Requirement manifests are JSON standard-input data, never R source.
        let mut child = command.spawn().map_err(|error| {
            format!(
                "failed to run managed Python resolver with `{}`: {error}",
                rscript.display()
            )
        })?;
        let stdout = read_output(child.stdout.take().expect("resolver stdout is piped"));
        let stderr = read_output(child.stderr.take().expect("resolver stderr is piped"));
        let input = child.stdin.take().expect("resolver stdin is piped");
        let (events, event_receiver) = mpsc::channel();
        let stop_handle = ResolverStopHandle(events.clone());
        if let Err(error) = on_started(stop_handle) {
            let _ = stop_resolver(&mut child, &rscript);
            return Err(error);
        }
        watch_resolver_exit(child.id(), events);
        let input = write_requirements(input, &requirements);
        let ResolverOutput {
            status,
            write_result,
            stdout,
            stderr,
        } = wait_for_resolver(&mut child, event_receiver, input, stdout, stderr, &rscript)?;
        if !status.success() {
            let python = String::from_utf8_lossy(&stdout);
            let error = String::from_utf8_lossy(&stderr);
            let python = python.trim();
            let error = error.trim();
            return if python.is_empty() {
                Err(format!(
                    "managed Python resolution failed with {status}: {error}"
                ))
            } else {
                let packages = requirements.packages.iter().map(String::as_str).collect();
                let python_version = requirements
                    .python_version
                    .iter()
                    .map(String::as_str)
                    .collect();
                let input = serde_json::to_string_pretty(&ResolverInput {
                    python,
                    packages,
                    python_version,
                    exclude_newer: requirements.exclude_newer.as_deref(),
                })
                .expect("resolver input strings should serialize as JSON");
                Err(format!(
                    "managed Python resolution failed:\nresolver input:\n{input}\nuv output:\n{error}"
                ))
            };
        }
        write_result.map_err(|error| format!("failed to write Python requirements: {error}"))?;

        let output = String::from_utf8(stdout)
            .map_err(|_| "managed Python resolver returned a non-UTF-8 path".to_string())?;
        let python = PathBuf::from(output.trim());
        if !python.is_absolute() || !python.is_file() {
            return Err(format!(
                "managed Python resolver returned invalid interpreter `{}`",
                python.display()
            ));
        }
        Ok(ManagedPython {
            python,
            requirements,
        })
    }

    fn write_requirements(
        mut input: ChildStdin,
        requirements: &crate::worker_protocol::PythonRequirementManifest,
    ) -> Receiver<io::Result<()>> {
        let bytes = serde_json::to_vec(requirements)
            .expect("managed Python requirements should serialize as JSON");
        let (sender, receiver) = mpsc::channel();
        let _ = thread::spawn(move || {
            let _ = sender.send(input.write_all(&bytes));
        });
        receiver
    }

    fn manifest_from_packages(
        requirements: &[String],
    ) -> crate::worker_protocol::PythonRequirementManifest {
        crate::worker_protocol::PythonRequirementManifest {
            packages: std::iter::once("numpy".to_string())
                .chain(requirements.iter().cloned())
                .collect(),
            python_version: Vec::new(),
            exclude_newer: None,
        }
        .normalized()
    }

    fn uv_environment() -> BTreeMap<String, String> {
        std::env::vars_os()
            .filter_map(|(name, value)| Some((name.into_string().ok()?, value.into_string().ok()?)))
            .filter(|(name, _)| name.starts_with("UV_") && name != "UV_OFFLINE")
            .collect()
    }

    fn validate_environment(environment: &BTreeMap<String, String>) -> Result<(), String> {
        if let Some(name) = environment
            .keys()
            .find(|name| !name.starts_with("UV_") || name.as_str() == "UV_OFFLINE")
        {
            return Err(format!(
                "managed Python resolver received unsupported environment variable `{name}`"
            ));
        }
        Ok(())
    }

    fn read_output(mut output: impl io::Read + Send + 'static) -> Receiver<io::Result<Vec<u8>>> {
        let (sender, receiver) = mpsc::channel();
        let _ = thread::spawn(move || {
            let mut bytes = Vec::new();
            let result = output.read_to_end(&mut bytes).map(|_| bytes);
            let _ = sender.send(result);
        });
        receiver
    }

    fn watch_resolver_exit(pid: u32, events: Sender<ResolverEvent>) {
        let _ = thread::spawn(move || {
            let result = loop {
                let mut status = MaybeUninit::<libc::siginfo_t>::uninit();
                // SAFETY: `status` points to writable storage and `pid` identifies
                // the direct child. `WNOWAIT` leaves its status for `Child::wait`.
                let result = unsafe {
                    libc::waitid(
                        libc::P_PID,
                        pid as libc::id_t,
                        status.as_mut_ptr(),
                        libc::WEXITED | libc::WNOWAIT,
                    )
                };
                if result == 0 {
                    break Ok(());
                }
                let error = io::Error::last_os_error();
                if error.kind() != io::ErrorKind::Interrupted {
                    break Err(error);
                }
            };
            let _ = events.send(ResolverEvent::Exited(result));
        });
    }

    fn receive_result<T>(
        receiver: Receiver<io::Result<T>>,
        name: &str,
    ) -> Result<io::Result<T>, String> {
        receiver
            .recv()
            .map_err(|_| format!("managed Python resolver {name} task stopped"))
    }

    fn wait_for_resolver_exit(
        child: &mut Child,
        events: Receiver<ResolverEvent>,
        rscript: &Path,
    ) -> Result<ExitStatus, String> {
        match events.recv() {
            Ok(ResolverEvent::Cancel) => {
                stop_resolver(child, rscript)?;
                Err("managed Python resolution cancelled".to_string())
            }
            Ok(ResolverEvent::Exited(Ok(()))) => stop_resolver(child, rscript),
            Ok(ResolverEvent::Exited(Err(error))) => {
                let _ = stop_resolver(child, rscript);
                Err(format!(
                    "failed to wait for managed Python resolver `{}`: {error}",
                    rscript.display()
                ))
            }
            Err(_) => {
                let _ = stop_resolver(child, rscript);
                Err("managed Python resolver exit task stopped".to_string())
            }
        }
    }

    fn wait_for_resolver(
        child: &mut Child,
        events: Receiver<ResolverEvent>,
        input: Receiver<io::Result<()>>,
        stdout: Receiver<io::Result<Vec<u8>>>,
        stderr: Receiver<io::Result<Vec<u8>>>,
        rscript: &Path,
    ) -> Result<ResolverOutput, String> {
        let status = wait_for_resolver_exit(child, events, rscript)?;
        let write_result = receive_result(input, "stdin writer")?;
        let stdout = receive_result(stdout, "stdout reader")?
            .map_err(|error| format!("failed to read resolver stdout: {error}"))?;
        let stderr = receive_result(stderr, "stderr reader")?
            .map_err(|error| format!("failed to read resolver stderr: {error}"))?;
        Ok(ResolverOutput {
            status,
            write_result,
            stdout,
            stderr,
        })
    }

    impl ResolverStopHandle {
        pub(crate) fn stop(&self) -> Result<(), String> {
            let _ = self.0.send(ResolverEvent::Cancel);
            Ok(())
        }
    }

    fn stop_resolver(child: &mut Child, rscript: &Path) -> Result<ExitStatus, String> {
        // SAFETY: `process_group(0)` made the resolver PID its process-group ID.
        let result = unsafe { libc::killpg(child.id() as libc::pid_t, libc::SIGKILL) };
        if result < 0 {
            let kill_error = io::Error::last_os_error();
            return match child.try_wait() {
                // macOS reports EPERM when only the unreaped group leader remains.
                // ESRCH likewise means there is no remaining group to stop.
                Ok(Some(status))
                    if matches!(
                        kill_error.raw_os_error(),
                        Some(libc::EPERM) | Some(libc::ESRCH)
                    ) =>
                {
                    Ok(status)
                }
                Ok(Some(_)) => Err(format!(
                    "failed to stop managed Python resolver `{}`: {kill_error}",
                    rscript.display()
                )),
                Ok(None) => Err(format!(
                    "failed to stop managed Python resolver `{}`: {kill_error}",
                    rscript.display()
                )),
                Err(wait_error) => Err(format!(
                    "failed to stop managed Python resolver `{}`: {kill_error}; additionally failed to read its status: {wait_error}",
                    rscript.display()
                )),
            };
        }
        child.wait().map_err(|error| {
            format!(
                "failed to reap managed Python resolver `{}`: {error}",
                rscript.display()
            )
        })
    }
}

#[cfg(not(target_os = "macos"))]
mod platform {
    use std::collections::BTreeMap;

    #[derive(Clone)]
    pub(crate) struct ManagedPython {
        requirements: crate::worker_protocol::PythonRequirementManifest,
    }
    #[derive(Clone)]
    pub(crate) struct ResolverStopHandle;

    impl ResolverStopHandle {
        pub(crate) fn stop(&self) -> Result<(), String> {
            Ok(())
        }
    }

    impl ManagedPython {
        pub(crate) fn requirements(&self) -> &crate::worker_protocol::PythonRequirementManifest {
            &self.requirements
        }
    }

    pub(crate) fn resolve_python(
        requirements: &[String],
        _on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<Option<ManagedPython>, String> {
        if requirements.is_empty() {
            Ok(None)
        } else {
            Err("managed Python environments are supported only on macOS".to_string())
        }
    }

    pub(crate) fn resolve_python_manifest(
        _requirements: crate::worker_protocol::PythonRequirementManifest,
        _environment: BTreeMap<String, String>,
        _on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<ManagedPython, String> {
        Err("managed Python environments are supported only on macOS".to_string())
    }
}

pub(crate) use platform::{
    ManagedPython, ResolverStopHandle, resolve_python, resolve_python_manifest,
};
