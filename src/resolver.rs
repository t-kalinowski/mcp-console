#[cfg(target_os = "macos")]
mod platform {
    use std::io::{self, Write};
    use std::os::unix::process::CommandExt as _;
    use std::path::PathBuf;
    use std::process::{Child, ChildStdin, Command, ExitStatus, Stdio};
    use std::sync::mpsc::{self, Receiver, TryRecvError};
    use std::thread;
    use std::time::Duration;

    use serde::Serialize;

    const CANCEL_POLL_INTERVAL: Duration = Duration::from_millis(10);

    const PYTHON_RESOLVER: &str = r#"
base::local({
  requirements <- base::readLines(
    base::file("stdin", encoding = "UTF-8"),
    warn = FALSE
  )
  requirements <- base::unique(c("numpy", requirements))
  messages <- utils::capture.output(
    ignored_output <- utils::capture.output(
      python <- base::try(
        reticulate:::uv_get_or_create_env(packages = requirements),
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

    pub(crate) struct ManagedPython {
        python: PathBuf,
    }

    #[derive(Clone)]
    pub(crate) struct ResolverStopHandle(mpsc::Sender<()>);

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
    }

    impl ManagedPython {
        pub(crate) fn configure_worker(&self, command: &mut crate::sandbox::SandboxedCommand) {
            command.env("RETICULATE_PYTHON", &self.python);
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
        let rscript = std::env::var_os("R_HOME")
            .map(|r_home| PathBuf::from(r_home).join("bin/Rscript"))
            .unwrap_or_else(|| PathBuf::from("Rscript"));
        let mut command = Command::new(&rscript);
        command
            .args(["--vanilla", "-e", PYTHON_RESOLVER])
            .env_remove("UV_OFFLINE")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .process_group(0);
        // Managed resolution intentionally runs before the sandboxed worker starts
        // because reticulate and uv need normal host network and cache access.
        // Explicitly prepared requirement strings are newline-delimited stdin data.
        let mut child = command.spawn().map_err(|error| {
            format!(
                "failed to run managed Python resolver with `{}`: {error}",
                rscript.display()
            )
        })?;
        let stdout = read_output(child.stdout.take().expect("resolver stdout is piped"));
        let stderr = read_output(child.stderr.take().expect("resolver stderr is piped"));
        let input = child.stdin.take().expect("resolver stdin is piped");
        let (cancel, cancellation) = mpsc::channel();
        let stop_handle = ResolverStopHandle(cancel);
        if let Err(error) = on_started(stop_handle.clone()) {
            let _ = stop_resolver(&mut child, &rscript);
            return Err(error);
        }
        let input = write_requirements(input, requirements);
        let ResolverOutput {
            status,
            write_result,
            stdout,
            stderr,
        } = wait_for_resolver(&mut child, cancellation, input, stdout, stderr, &rscript)?;
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
                let packages = std::iter::once("numpy")
                    .chain(
                        requirements
                            .iter()
                            .map(String::as_str)
                            .filter(|requirement| *requirement != "numpy"),
                    )
                    .collect();
                let input = serde_json::to_string_pretty(&ResolverInput { python, packages })
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
        Ok(Some(ManagedPython { python }))
    }

    fn write_requirements(
        mut input: ChildStdin,
        requirements: &[String],
    ) -> Receiver<io::Result<()>> {
        let mut bytes = requirements.join("\n").into_bytes();
        if !bytes.is_empty() {
            bytes.push(b'\n');
        }
        let (sender, receiver) = mpsc::channel();
        let _ = thread::spawn(move || {
            let _ = sender.send(input.write_all(&bytes));
        });
        receiver
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

    fn receive_result<T>(
        receiver: &Receiver<io::Result<T>>,
        output: &mut Option<io::Result<T>>,
        name: &str,
        child: &mut Child,
        rscript: &std::path::Path,
    ) -> Result<(), String> {
        if output.is_some() {
            return Ok(());
        }
        match receiver.try_recv() {
            Ok(result) => *output = Some(result),
            Err(TryRecvError::Empty) => {}
            Err(TryRecvError::Disconnected) => {
                let _ = stop_resolver(child, rscript);
                return Err(format!("managed Python resolver {name} task stopped"));
            }
        }
        Ok(())
    }

    fn wait_for_resolver(
        child: &mut Child,
        cancellation: Receiver<()>,
        input: Receiver<io::Result<()>>,
        stdout: Receiver<io::Result<Vec<u8>>>,
        stderr: Receiver<io::Result<Vec<u8>>>,
        rscript: &std::path::Path,
    ) -> Result<ResolverOutput, String> {
        let mut input_result = None;
        let mut stdout_output = None;
        let mut stderr_output = None;
        loop {
            match cancellation.try_recv() {
                Ok(()) => {
                    stop_resolver(child, rscript)?;
                    return Err("managed Python resolution cancelled".to_string());
                }
                Err(TryRecvError::Empty | TryRecvError::Disconnected) => {}
            }
            receive_result(&input, &mut input_result, "stdin writer", child, rscript)?;
            receive_result(&stdout, &mut stdout_output, "stdout reader", child, rscript)?;
            receive_result(&stderr, &mut stderr_output, "stderr reader", child, rscript)?;
            if input_result.is_none() || stdout_output.is_none() || stderr_output.is_none() {
                thread::sleep(CANCEL_POLL_INTERVAL);
                continue;
            }
            let status = match child.try_wait() {
                Ok(status) => status,
                Err(error) => {
                    let _ = stop_resolver(child, rscript);
                    return Err(format!(
                        "failed to collect managed Python resolver output from `{}`: {error}",
                        rscript.display()
                    ));
                }
            };
            if let Some(status) = status {
                let stdout = stdout_output
                    .expect("resolver stdout is available")
                    .map_err(|error| format!("failed to read resolver stdout: {error}"))?;
                let stderr = stderr_output
                    .expect("resolver stderr is available")
                    .map_err(|error| format!("failed to read resolver stderr: {error}"))?;
                return Ok(ResolverOutput {
                    status,
                    write_result: input_result.expect("resolver stdin result is available"),
                    stdout,
                    stderr,
                });
            }
            thread::sleep(CANCEL_POLL_INTERVAL);
        }
    }

    impl ResolverStopHandle {
        pub(crate) fn stop(&self) -> Result<(), String> {
            let _ = self.0.send(());
            Ok(())
        }
    }

    fn stop_resolver(child: &mut Child, rscript: &std::path::Path) -> Result<(), String> {
        // SAFETY: `process_group(0)` made the resolver PID its process-group ID.
        let result = unsafe { libc::killpg(child.id() as libc::pid_t, libc::SIGKILL) };
        if result < 0 {
            let kill_error = io::Error::last_os_error();
            return match child.try_wait() {
                Ok(Some(_)) => Ok(()),
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
        child.wait().map(|_| ()).map_err(|error| {
            format!(
                "failed to reap managed Python resolver `{}`: {error}",
                rscript.display()
            )
        })
    }
}

#[cfg(not(target_os = "macos"))]
mod platform {
    pub(crate) struct ManagedPython;
    #[derive(Clone)]
    pub(crate) struct ResolverStopHandle;

    impl ResolverStopHandle {
        pub(crate) fn stop(&self) -> Result<(), String> {
            Ok(())
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
}

pub(crate) use platform::{ManagedPython, ResolverStopHandle, resolve_python};
