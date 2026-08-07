#[cfg(target_os = "macos")]
mod platform {
    use std::io::{self, Write};
    use std::os::unix::process::CommandExt as _;
    use std::path::PathBuf;
    use std::process::{Child, ChildStdin, Command, ExitStatus, Stdio};
    use std::sync::mpsc::{self, Receiver, TryRecvError};
    use std::thread;
    use std::time::Duration;

    const CANCEL_POLL_INTERVAL: Duration = Duration::from_millis(10);

    const PREFLIGHT: &str = r#"
base::local({
  input <- base::file("stdin", open = "r", encoding = "UTF-8")
  requirements <- base::readLines(input, warn = FALSE)
  base::close(input)
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
    command <- messages[
      base::grepl(" tool run --isolated ", messages, fixed = TRUE)
    ]
    uv_error <- base::any(base::startsWith(messages, "uv error code: "))
    if (uv_error && base::length(command)) {
      base::cat(command[[1L]], "\n", sep = "")
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

    const BRIDGE_INIT: &str = r#"
base::local({
  evaluator <- NULL
  source <- NULL

  evaluate <- function(id) {
    if (is.null(evaluator)) {
      private <- reticulate::py_run_string(r"---(
import __main__ as _main
import ast as _ast
import builtins as _builtins
import sys as _sys
import traceback as _traceback


def _mcp_console_eval_cell(
    source,
    filename,
    _main=_main,
    _parse=_ast.parse,
    _Expr=_ast.Expr,
    _Expression=_ast.Expression,
    _isinstance=_builtins.isinstance,
    _compile=_builtins.compile,
    _exec=_builtins.exec,
    _eval=_builtins.eval,
    _BaseException=_builtins.BaseException,
    _sys=_sys,
    _print_exc=_traceback.print_exc,
):
    try:
        module = _parse(source, filename=filename, mode="exec")
        final = module.body[-1] if module.body else None
        if _isinstance(final, _Expr):
            module.body.pop()
            statements = _compile(module, filename, "exec") if module.body else None
            expression = _compile(_Expression(final.value), filename, "eval")
        else:
            statements = _compile(module, filename, "exec")
            expression = None

        if statements is not None:
            _exec(statements, _main.__dict__)
        if expression is not None:
            _sys.displayhook(_eval(expression, _main.__dict__))
    except _BaseException:
        _print_exc()
)---", local = TRUE, convert = FALSE)
      evaluator <<- private$`_mcp_console_eval_cell`
    }

    filename <- paste0("<mcp-console:python:", id, ">")
    invisible(evaluator(source, filename))
  }

  environment()
}, envir = base::new.env(parent = base::baseenv()))
"#;

    pub(crate) struct Bridge(crate::r_bridge::Bridge);

    pub(crate) struct Managed {
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

    impl Managed {
        pub(crate) fn configure_worker(&self, command: &mut crate::sandbox::SandboxedCommand) {
            command.env("RETICULATE_PYTHON", &self.python);
        }
    }

    impl Bridge {
        pub(crate) fn initialize() -> Result<Self, String> {
            crate::r_bridge::Bridge::initialize(BRIDGE_INIT, "Python").map(Self)
        }

        pub(crate) fn evaluate(&mut self, source: &str) -> Result<(), String> {
            self.0.evaluate(source)
        }
    }

    pub(crate) fn configure_worker_environment() -> io::Result<()> {
        for (name, value) in [
            (c"RETICULATE_REMAP_OUTPUT_STREAMS", c"1"),
            (c"UV_OFFLINE", c"1"),
        ] {
            if unsafe { libc::setenv(name.as_ptr(), value.as_ptr(), 1) } != 0 {
                return Err(io::Error::last_os_error());
            }
        }
        Ok(())
    }

    pub(crate) fn preflight(
        requirements: &[String],
        on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<Option<Managed>, String> {
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
            .args(["--vanilla", "-e", PREFLIGHT])
            .env_remove("UV_OFFLINE")
            .env("_RETICULATE_DEBUG_UV_", "1")
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
            let command = String::from_utf8_lossy(&stdout);
            let error = String::from_utf8_lossy(&stderr);
            let command = command.trim();
            let error = error.trim();
            return if command.is_empty() {
                Err(format!(
                    "managed Python resolution failed with {status}: {error}"
                ))
            } else {
                Err(format!(
                    "managed Python resolution failed:\nuv command:\n{command}\nuv output:\n{error}"
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
        Ok(Some(Managed { python }))
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
    pub(crate) struct Managed;
    #[derive(Clone)]
    pub(crate) struct ResolverStopHandle;

    impl ResolverStopHandle {
        pub(crate) fn stop(&self) -> Result<(), String> {
            Ok(())
        }
    }

    pub(crate) fn preflight(
        requirements: &[String],
        _on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<Option<Managed>, String> {
        if requirements.is_empty() {
            Ok(None)
        } else {
            Err("managed Python environments are supported only on macOS".to_string())
        }
    }
}

#[cfg(target_os = "macos")]
pub(crate) use platform::{Bridge, configure_worker_environment};
pub(crate) use platform::{Managed, ResolverStopHandle, preflight};
