#[cfg(target_os = "macos")]
mod platform {
    use std::ffi::c_int;
    use std::io;
    use std::path::PathBuf;
    use std::process::{Command, Stdio};

    const PREFLIGHT: &str =
        r#"Sys.setenv(RETICULATE_PYTHON = "managed"); cat(reticulate::py_config()$python)"#;

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

    type TryEval = unsafe extern "C-unwind" fn(libr::SEXP, libr::SEXP, *mut c_int) -> libr::SEXP;

    pub(crate) struct Bridge {
        state: libr::SEXP,
        try_eval: TryEval,
        next_evaluation_id: u64,
    }

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
            let library = libloading::os::unix::Library::this();
            let try_eval = unsafe {
                *library
                    .get::<TryEval>(b"R_tryEval\0")
                    .map_err(|error| format!("failed to load R_tryEval: {error}"))?
            };
            let source_length = c_int::try_from(BRIDGE_INIT.len())
                .expect("the fixed Python bridge should fit in an R string");
            let (evaluation_error, state, is_environment) = harp::top_level_exec(|| {
                // SAFETY: This runs on R's main thread. The top-level boundary
                // contains allocation failures, and R_tryEval contains errors raised
                // while R parses and evaluates the fixed bridge source.
                unsafe {
                    let source = libr::Rf_protect(r_string(BRIDGE_INIT, source_length));
                    let str2expression = libr::Rf_install(c"str2expression".as_ptr());
                    let call = libr::Rf_protect(libr::Rf_lang2(str2expression, source));
                    let eval = libr::Rf_install(c"eval".as_ptr());
                    let call = libr::Rf_protect(libr::Rf_lang2(eval, call));
                    let mut evaluation_error = 0;
                    let state = try_eval(call, libr::R_BaseEnv, &mut evaluation_error);
                    if evaluation_error != 0 || state.is_null() {
                        libr::Rf_unprotect(3);
                        return (evaluation_error, state, false);
                    }
                    let state = libr::Rf_protect(state);
                    let is_environment = libr::TYPEOF(state) == libr::ENVSXP as c_int;
                    if is_environment {
                        libr::R_PreserveObject(state);
                    }
                    libr::Rf_unprotect(4);
                    (evaluation_error, state, is_environment)
                }
            })
            .map_err(|error| format!("failed to initialize the Python bridge: {error}"))?;
            if evaluation_error != 0 {
                return Err("Python bridge initialization failed during R evaluation".to_string());
            }
            if state.is_null() {
                return Err("Python state initialization returned a null R object".to_string());
            }
            if !is_environment {
                return Err(
                    "Python state initialization did not produce an environment".to_string()
                );
            }
            Ok(Self {
                state,
                try_eval,
                next_evaluation_id: 1,
            })
        }

        pub(crate) fn evaluate(&mut self, source: &str) -> Result<(), String> {
            let source_length = c_int::try_from(source.len())
                .map_err(|_| "Python source exceeds R's maximum string size".to_string())?;
            let evaluation_id = format!("e{}", self.next_evaluation_id);
            self.next_evaluation_id += 1;
            let evaluation_id_length = c_int::try_from(evaluation_id.len())
                .expect("generated evaluation IDs should fit in an R string");
            let result = harp::top_level_exec(|| {
                // SAFETY: This runs on R's main thread. The outer top-level
                // boundary contains allocation errors; R_tryEval contains errors
                // raised while the preserved private environment calls reticulate.
                unsafe {
                    let source = libr::Rf_protect(r_string(source, source_length));
                    let evaluation_id =
                        libr::Rf_protect(r_string(&evaluation_id, evaluation_id_length));
                    let source_symbol = libr::Rf_install(c"source".as_ptr());
                    let evaluate_symbol = libr::Rf_install(c"evaluate".as_ptr());
                    libr::Rf_defineVar(source_symbol, source, self.state);
                    let call = libr::Rf_protect(libr::Rf_lang2(evaluate_symbol, evaluation_id));
                    let mut evaluation_error = 0;
                    (self.try_eval)(call, self.state, &mut evaluation_error);
                    libr::Rf_defineVar(source_symbol, libr::R_NilValue, self.state);
                    libr::Rf_unprotect(3);
                    evaluation_error
                }
            });
            let evaluation_error =
                result.map_err(|error| format!("failed to call the Python bridge: {error}"))?;
            if evaluation_error != 0 {
                return Err("Python bridge failed during R evaluation".to_string());
            }
            Ok(())
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

    pub(crate) fn preflight() -> Result<Option<Managed>, String> {
        if std::env::var_os("RETICULATE_PYTHON").is_some_and(|value| value != "managed") {
            return Ok(None);
        }

        let rscript = std::env::var_os("R_HOME")
            .map(|r_home| PathBuf::from(r_home).join("bin/Rscript"))
            .unwrap_or_else(|| PathBuf::from("Rscript"));
        let mut command = Command::new(&rscript);
        command
            .args(["--vanilla", "-e", PREFLIGHT])
            .env_remove("UV_OFFLINE")
            .stdin(Stdio::null());
        // Managed resolution intentionally runs before the sandboxed worker starts:
        // reticulate and uv need normal host network and cache access, and no MCP
        // input is accepted before this command completes.
        let output = command.output().map_err(|error| {
            format!(
                "failed to run managed Python preflight with `{}`: {error}",
                rscript.display()
            )
        })?;
        if !output.status.success() {
            let error = String::from_utf8_lossy(&output.stderr);
            return Err(format!(
                "managed Python preflight failed with {}: {}",
                output.status,
                error.trim()
            ));
        }

        let output = String::from_utf8(output.stdout)
            .map_err(|_| "managed Python preflight returned a non-UTF-8 path".to_string())?;
        let python = PathBuf::from(output.trim());
        if !python.is_absolute() || !python.is_file() {
            return Err(format!(
                "managed Python preflight returned invalid interpreter `{}`",
                python.display()
            ));
        }
        Ok(Some(Managed { python }))
    }

    fn r_string(value: &str, length: c_int) -> libr::SEXP {
        // SAFETY: The caller runs under R's top-level allocation boundary and
        // immediately protects the returned scalar string.
        unsafe {
            let value = libr::Rf_mkCharLenCE(value.as_ptr().cast(), length, libr::cetype_t_CE_UTF8);
            libr::Rf_ScalarString(value)
        }
    }
}

#[cfg(not(target_os = "macos"))]
mod platform {
    pub(crate) struct Managed;

    pub(crate) fn preflight() -> Result<Option<Managed>, String> {
        Ok(None)
    }
}

#[cfg(target_os = "macos")]
pub(crate) use platform::{Bridge, configure_worker_environment};
pub(crate) use platform::{Managed, preflight};
