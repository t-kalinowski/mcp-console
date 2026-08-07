#[cfg(target_os = "macos")]
mod platform {
    use std::io;

    use libr::SEXP;

    const BRIDGE_INIT: &str = r#"
base::local({
  evaluator <- NULL
  managed <- Sys.getenv("MCP_CONSOLE_MANAGED_PYTHON", unset = NA_character_)
  source <- NULL

  manifest <- function(packages, python_version, exclude_newer) {
    list(
      packages = I(sort(unique(packages %||% character()))),
      python_version = I(sort(unique(python_version %||% character()))),
      exclude_newer = exclude_newer
    )
  }

  uv_environment <- function() {
    environment <- Sys.getenv()
    as.list(environment[
      startsWith(names(environment), "UV_") &
        names(environment) != "UV_OFFLINE"
    ])
  }

  request_json <- function(requirements) {
    jsonlite::toJSON(
      list(
        requirements = requirements,
        environment = uv_environment()
      ),
      auto_unbox = TRUE,
      null = "null",
      na = "null"
    )
  }

  install_managed_python <- function(...) {
    namespace <- asNamespace("reticulate")
    current_requirement <- function(name) {
      get("py_reqs_get", envir = namespace)(name)
    }
    resolve <- function(
      packages = current_requirement("packages"),
      python_version = current_requirement("python_version"),
      exclude_newer = current_requirement("exclude_newer")
    ) {
      requirements <- manifest(packages, python_version, exclude_newer)
      .Call(
        "mcp_console_resolve_python",
        request_json(requirements)
      )
    }

    seed <- jsonlite::fromJSON(managed)
    packages <- unlist(seed$packages, use.names = FALSE)
    python_version <- unlist(seed$python_version, use.names = FALSE)
    if (!length(python_version)) {
      python_version <- NULL
    }
    globals <- get(".globals", envir = namespace)
    requirements <- get("py_reqs_get", envir = namespace)()
    changed <- !identical(
      manifest(
        requirements$packages,
        requirements$python_version,
        requirements$exclude_newer
      ),
      manifest(packages, python_version, seed$exclude_newer)
    )
    if (changed) {
      requirements$packages <- packages
      requirements$python_version <- python_version
      requirements$exclude_newer <- seed$exclude_newer
      requirements$history <- c(requirements$history, list(list(
        requested_from = "mcp-console",
        env_is_package = FALSE,
        packages = packages,
        python_version = python_version,
        exclude_newer = seed$exclude_newer,
        action = "set"
      )))
      globals$python_requirements <- requirements
    }

    replace_binding <- function(name, value) {
      was_locked <- bindingIsLocked(name, namespace)
      if (was_locked) {
        unlockBinding(name, namespace)
      }
      on.exit(
        if (was_locked) lockBinding(name, namespace),
        add = TRUE
      )
      assign(name, value, envir = namespace)
      invisible()
    }
    replace_binding("uv_get_or_create_env", resolve)
    invisible()
  }

  if (!is.na(managed)) {
    Sys.unsetenv("MCP_CONSOLE_MANAGED_PYTHON")
    `%||%` <- function(x, y) if (is.null(x)) y else x
    setHook(
      packageEvent("reticulate", "onLoad"),
      install_managed_python,
      action = "append"
    )
    if ("reticulate" %in% loadedNamespaces()) {
      install_managed_python()
    }
  }

  checkpoint <- function() {
    if (is.na(managed) || !"reticulate" %in% loadedNamespaces()) {
      return(NA_character_)
    }
    namespace <- asNamespace("reticulate")
    requirements <- reticulate::py_require()
    initialized <- get("is_python_initialized", envir = namespace)()
    if (!initialized) {
      invisible(get("uv_get_or_create_env", envir = namespace)(
        requirements$packages,
        requirements$python_version,
        requirements$exclude_newer
      ))
    }
    jsonlite::toJSON(
      manifest(
        requirements$packages,
        requirements$python_version,
        requirements$exclude_newer
      ),
      auto_unbox = TRUE,
      null = "null",
      na = "null"
    )
  }

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

    impl Bridge {
        pub(crate) fn initialize() -> Result<Self, String> {
            crate::r_bridge::Bridge::initialize(BRIDGE_INIT, "Python").map(Self)
        }

        pub(crate) fn evaluate(&mut self, source: &str) -> Result<(), String> {
            self.0.evaluate(source)
        }

        pub(crate) fn checkpoint(
            &self,
        ) -> Result<Option<crate::worker_protocol::PythonRequirementManifest>, String> {
            self.0
                .call0_string(c"checkpoint")?
                .map(|checkpoint| {
                    serde_json::from_str(&checkpoint)
                        .map_err(|error| format!("invalid Python checkpoint: {error}"))
                })
                .transpose()
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

    #[allow(clippy::result_large_err)]
    #[harp::register]
    pub extern "C-unwind" fn mcp_console_resolve_python(request: SEXP) -> harp::Result<SEXP> {
        let request = String::try_from(harp::object::RObject::view(request))?;
        let request = serde_json::from_str(&request).map_err(|error| harp::anyhow!("{error}"))?;
        let python =
            crate::worker::resolve_python(request).map_err(|error| harp::anyhow!("{error}"))?;
        Ok(harp::object::RObject::from(python).sexp)
    }
}

#[cfg(target_os = "macos")]
pub(crate) use platform::{Bridge, configure_worker_environment};
