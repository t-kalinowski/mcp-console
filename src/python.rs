#[cfg(target_os = "macos")]
mod platform {
    use std::io;
    use std::path::PathBuf;
    use std::process::{Command, Stdio};

    const PREFLIGHT: &str = r#"
base::local({
  arguments <- base::commandArgs(trailingOnly = TRUE)
  base::stopifnot(base::identical(arguments[[1L]], "--args"))
  requirements <- base::unique(c(
    "numpy",
    arguments[-1L]
  ))
  python <- reticulate:::uv_get_or_create_env(packages = requirements)
  base::stopifnot(
    base::length(python) == 1L,
    !base::is.na(python),
    base::nzchar(python)
  )
  base::cat(python)
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

    pub(crate) fn preflight(requirements: &[String]) -> Result<Option<Managed>, String> {
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
            // Requirement strings are untrusted; stop Rscript option parsing
            // before passing them as ordinary arguments.
            .arg("--args")
            .args(requirements)
            .env_remove("UV_OFFLINE")
            .stdin(Stdio::null());
        // Managed resolution intentionally runs before the sandboxed worker starts
        // because reticulate and uv need normal host network and cache access.
        // Explicitly prepared requirement strings remain argv data, not R expressions.
        let output = command.output().map_err(|error| {
            format!(
                "failed to run managed Python resolver with `{}`: {error}",
                rscript.display()
            )
        })?;
        if !output.status.success() {
            let error = String::from_utf8_lossy(&output.stderr);
            return Err(format!(
                "managed Python resolution failed with {}: {}",
                output.status,
                error.trim()
            ));
        }

        let output = String::from_utf8(output.stdout)
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
}

#[cfg(not(target_os = "macos"))]
mod platform {
    pub(crate) struct Managed;

    pub(crate) fn preflight(requirements: &[String]) -> Result<Option<Managed>, String> {
        if requirements.is_empty() {
            Ok(None)
        } else {
            Err("managed Python environments are supported only on macOS".to_string())
        }
    }
}

#[cfg(target_os = "macos")]
pub(crate) use platform::{Bridge, configure_worker_environment};
pub(crate) use platform::{Managed, preflight};
