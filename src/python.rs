#[cfg(target_os = "macos")]
mod platform {
    use std::ffi::{CStr, CString};
    use std::fs;
    use std::io;
    use std::os::unix::ffi::OsStrExt as _;
    use std::os::unix::fs::symlink;
    use std::path::{Path, PathBuf};
    use std::sync::OnceLock;

    use libr::SEXP;

    static MATPLOTLIB_DIRECTORY: OnceLock<PathBuf> = OnceLock::new();
    static INHERITED_MATPLOTLIB_DIRECTORY: OnceLock<PathBuf> = OnceLock::new();

    const BRIDGE_INIT: &str = r#"
base::local({
  initialized <- FALSE
  managed <- Sys.getenv("MCP_CONSOLE_MANAGED_PYTHON", unset = NA_character_)
  # Python 3.9 and older are intentionally outside the bridge contract.
  minimum_python <- base::numeric_version("3.10")
  # Reticulate callable proxies convert results through an interruptible wrapper.
  # Keep helpers in one module, then use py_eval's direct conversion path.
  python_dispatch <-
    "(lambda: None).__builtins__['__import__']('_mcp_console').dispatch()"
  python_module <- NULL
  pending_requirements <- NULL
  source <- NULL

  python_runtime_source <- r"---(
import __main__ as _main
import ast as _ast
import base64 as _base64
import builtins as _builtins
import io as _io
import logging as _logging
import sys as _sys
import traceback as _traceback
import types as _types


class _McpConsoleMatplotlibLogFilter(_logging.Filter):
    def filter(self, record):
        return record.getMessage() != (
            "Matplotlib is building the font cache; this may take a moment."
        )


_logging.getLogger("matplotlib.font_manager").addFilter(
    _McpConsoleMatplotlibLogFilter()
)

_mcp_console_image_state = [()]


def _mcp_console_disable_matplotlib_show(
    _setattr=_builtins.setattr,
    _sys=_sys,
):
    pyplot = _sys.modules.get("matplotlib.pyplot")
    if pyplot is not None:
        _setattr(pyplot, "show", lambda *args, **kwargs: None)
    return None


def _mcp_console_collect_plots(
    _BaseException=_builtins.BaseException,
    _base64=_base64,
    _io=_io,
    _print_exc=_traceback.print_exc,
    _sorted=_builtins.sorted,
    _sys=_sys,
):
    pyplot = _sys.modules.get("matplotlib.pyplot")
    if pyplot is None:
        return ()

    images = []
    try:
        for number in _sorted(pyplot.get_fignums()):
            if number not in pyplot.get_fignums():
                continue
            try:
                figure = pyplot.figure(number)
                output = _io.BytesIO()
                figure.savefig(output, format="png")
                images.append(_base64.b64encode(output.getvalue()).decode("ascii"))
            except _BaseException:
                _print_exc()
    finally:
        try:
            pyplot.close("all")
        except _BaseException:
            _print_exc()
    return tuple(images)


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
    _collect_plots=_mcp_console_collect_plots,
    _image_state=_mcp_console_image_state,
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
    try:
        _image_state[0] = _collect_plots()
    except _BaseException:
        _print_exc()
        _image_state[0] = ()
    return None


def _mcp_console_take_images(_image_state=_mcp_console_image_state):
    images = _image_state[0]
    _image_state[0] = ()
    return images


_mcp_console = _types.ModuleType("_mcp_console")


def _mcp_console_dispatch(state=_mcp_console.__dict__):
    operation = state.pop("operation")
    arguments = state.pop("arguments")
    return state[operation](*arguments)


_mcp_console.disable_matplotlib_show = _mcp_console_disable_matplotlib_show
_mcp_console.eval_cell = _mcp_console_eval_cell
_mcp_console.take_images = _mcp_console_take_images
_mcp_console.dispatch = _mcp_console_dispatch
_sys.modules[_mcp_console.__name__] = _mcp_console
)---"

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

  request_json <- function(requirements, retained_requirements) {
    jsonlite::toJSON(
      list(
        requirements = requirements,
        retained_requirements = retained_requirements,
        environment = uv_environment()
      ),
      auto_unbox = TRUE,
      null = "null",
      na = "null"
    )
  }

  version_request_json <- function(constraints) {
    jsonlite::toJSON(
      list(
        constraints = I(as.character(constraints %||% character())),
        environment = uv_environment()
      ),
      auto_unbox = TRUE,
      null = "null",
      na = "null"
    )
  }

  activation_json <- function(requirements) {
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

  report_activation <- function(requirements) {
    .Call("mcp_console_python_activated", activation_json(requirements))
    invisible()
  }

  install_managed_python <- function(...) {
    namespace <- asNamespace("reticulate")
    current_requirements <- function() {
      get("py_reqs_get", envir = namespace)()
    }
    resolve <- function(
      packages = current_requirements()$packages,
      python_version = get("py_reqs_python_version", envir = namespace)(),
      exclude_newer = current_requirements()$exclude_newer
    ) {
      current <- current_requirements()
      requirements <- manifest(packages, python_version, exclude_newer)
      retained_requirements <- manifest(
        packages,
        current$python_version,
        exclude_newer
      )
      .Call(
        "mcp_console_resolve_python",
        request_json(requirements, retained_requirements)
      )
    }
    resolve_version <- function(constraints = NULL, uv = NULL) {
      # The host resolver owns the executable; worker code supplies only
      # version constraints and supported UV settings.
      .Call(
        "mcp_console_resolve_python_version",
        version_request_json(constraints)
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
        exclude_newer_supplied = !is.null(seed$exclude_newer),
        action = "set"
      )))
      globals$python_requirements <- requirements
    }
    stopifnot(
      !bindingIsActive("python_requirements", globals),
      !bindingIsLocked("python_requirements", globals)
    )
    rm(list = "python_requirements", envir = globals)
    makeActiveBinding("python_requirements", function(value) {
      if (missing(value)) {
        return(requirements)
      }
      if (!is.null(pending_requirements)) {
        committed <- manifest(
          value$packages,
          value$python_version,
          value$exclude_newer
        )
        stopifnot(identical(committed, pending_requirements))
      }
      requirements <<- value
      if (!is.null(pending_requirements)) {
        pending_requirements <<- NULL
        report_activation(committed)
      }
      invisible(value)
    }, globals)

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
    original_activate <- get("py_reqs_activate", envir = namespace)
    activate <- function(requirements) {
      stopifnot(is.null(pending_requirements))
      config <- original_activate(requirements)
      pending_requirements <<- manifest(
        requirements$packages,
        requirements$python_version,
        requirements$exclude_newer
      )
      config
    }
    replace_binding("uv_get_or_create_env", resolve)
    replace_binding("resolve_python_version", resolve_version)
    replace_binding("py_reqs_activate", activate)
    setHook("reticulate.onPyInit", function() {
      report_activation(current_requirements())
    }, action = "append")
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

  dispatch_python <- function(operation, arguments = list()) {
    reticulate::py_set_attr(
      python_module, "operation", operation
    )
    reticulate::py_set_attr(
      python_module, "arguments", arguments
    )
    reticulate::py_eval(python_dispatch, convert = TRUE)
  }

  initialize_python_runtime <- function(strict = FALSE) {
    if (!is.null(python_module)) {
      return(invisible(TRUE))
    }

    python_config <- reticulate::py_config()
    if (!is.null(python_module)) {
      return(invisible(TRUE))
    }
    if (python_config$version < minimum_python) {
      if (!strict) {
        return(invisible(FALSE))
      }
      stop(
        paste0(
          "MCP Console requires Python 3.10 or later; selected ",
          "interpreter reports Python ",
          as.character(python_config$version)
        ),
        call. = FALSE
      )
    }
    reticulate::py_run_string(
      python_runtime_source,
      local = TRUE,
      convert = FALSE
    )
    python_module <<- reticulate::import("_mcp_console", convert = FALSE)
    base::setHook(
      "reticulate::matplotlib.pyplot::load",
      function(...) {
        invisible(dispatch_python("disable_matplotlib_show"))
      },
      action = "append"
    )
    invisible(TRUE)
  }

  console_width <- getOption("width")
  install_python_hooks <- function(...) {
    namespace <- asNamespace("reticulate")
    configure_numpy <- function() {
      numpy <- reticulate::import("numpy", convert = FALSE)
      numpy$set_printoptions(linewidth = console_width)
    }
    configure_pandas <- function() {
      pandas <- reticulate::import("pandas", convert = FALSE)
      pandas$set_option("display.width", console_width)
    }
    on_python_init <- function() {
      # Reticulate imports NumPy before its module-load hooks are installed.
      reticulate::py_register_load_hook("numpy", configure_numpy)
      reticulate::py_register_load_hook("pandas", configure_pandas)
      initialize_python_runtime(strict = FALSE)
    }
    base::setHook(
      "reticulate.onPyInit",
      on_python_init,
      action = "append"
    )
    if (get("is_python_initialized", envir = namespace)()) {
      on_python_init()
    }
    invisible()
  }
  setHook(
    packageEvent("reticulate", "onLoad"),
    install_python_hooks,
    action = "append"
  )
  if ("reticulate" %in% loadedNamespaces()) {
    install_python_hooks()
  }

  materialize_manifest <- function() {
    if (is.na(managed) || !"reticulate" %in% loadedNamespaces()) {
      return(NULL)
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
    manifest(
      requirements$packages,
      requirements$python_version,
      requirements$exclude_newer
    )
  }

  prepare <- function(request) {
    if (is.na(managed)) {
      stop("Python preparation requires a server-managed interpreter")
    }
    namespace <- asNamespace("reticulate")
    globals <- get(".globals", envir = namespace)
    snapshot <- get("py_reqs_get", envir = namespace)()
    result <- tryCatch({
      packages <- unlist(jsonlite::fromJSON(request), use.names = FALSE)
      reticulate::py_require(packages, action = "add")
      requirements <- materialize_manifest()
      if (is.null(requirements)) {
        stop("Python preparation did not produce a managed manifest")
      }
      list(kind = "prepared")
    }, error = function(error) {
      globals$python_requirements <- snapshot
      list(kind = "failed", message = conditionMessage(error))
    })
    jsonlite::toJSON(
      result,
      auto_unbox = TRUE,
      null = "null",
      na = "null"
    )
  }

  evaluate_impl <- function(id) {
    if (!initialized) {
      initialize_python_runtime(strict = TRUE)
      dispatch_python("disable_matplotlib_show")
      initialized <<- TRUE
    }

    filename <- paste0("<mcp-console:python:", id, ">")
    on.exit({
      images <- dispatch_python("take_images")
      for (image in images) {
        invisible(.Call("mcp_console_publish_python_plot", image))
      }
    }, add = TRUE)
    dispatch_python(
      "eval_cell",
      list(source, filename)
    )
    invisible()
  }

  interrupted <- FALSE

  evaluate <- function(id) {
    interrupted <<- FALSE
    # Observe the condition without handling it; R_tryEval remains the boundary.
    withCallingHandlers(
      evaluate_impl(id),
      interrupt = function(condition) interrupted <<- TRUE,
      error = function(condition) interrupted <<- FALSE
    )
  }

  environment()
}, envir = base::new.env(parent = base::baseenv()))
"#;

    #[derive(serde::Deserialize)]
    #[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
    pub(crate) enum PreparationOutcome {
        Prepared,
        Failed { message: String },
    }

    pub(crate) struct Bridge(crate::r_bridge::Bridge);

    impl Bridge {
        pub(crate) fn initialize() -> Result<Self, String> {
            crate::r_bridge::Bridge::initialize(BRIDGE_INIT, "Python").map(Self)
        }

        pub(crate) fn evaluate(&mut self, source: &str) -> Result<(), String> {
            self.0.evaluate(source)
        }

        pub(crate) fn prepare(&self, packages: Vec<String>) -> Result<PreparationOutcome, String> {
            let request = serde_json::to_string(&packages)
                .map_err(|error| format!("failed to serialize Python preparation: {error}"))?;
            let response = self
                .0
                .call1_string(c"prepare", &request)?
                .ok_or_else(|| "Python preparation bridge returned no response".to_string())?;
            serde_json::from_str(&response)
                .map_err(|error| format!("invalid Python preparation response: {error}"))
        }
    }

    pub(crate) fn configure_worker_environment() -> io::Result<()> {
        let matplotlib_cache_directory = inherited_matplotlib_directory();
        // Preserve the selected host configuration before redirecting all
        // Matplotlib writes to the worker's private directory.
        if let Some(config) = inherited_matplotlibrc(matplotlib_cache_directory.as_deref()) {
            let config = CString::new(config.as_os_str().as_bytes())
                .expect("Matplotlib configuration path should not contain NUL");
            set_environment(c"MATPLOTLIBRC", &config, true)?;
        }
        let temporary_directory = std::env::temp_dir();
        let matplotlib_directory = temporary_directory.join("matplotlib");
        MATPLOTLIB_DIRECTORY
            .set(matplotlib_directory.clone())
            .map_err(|_| io::Error::other("Matplotlib directory is already configured"))?;
        if let Some(cache) = matplotlib_cache_directory {
            let _ = INHERITED_MATPLOTLIB_DIRECTORY.set(cache);
        }
        link_matplotlib_caches();

        for (name, value, overwrite) in [
            (c"COLUMNS", c"200", true),
            (c"RETICULATE_REMAP_OUTPUT_STREAMS", c"1", true),
            (c"UV_OFFLINE", c"1", true),
            (c"MPLBACKEND", c"agg", false),
        ] {
            set_environment(name, value, overwrite)?;
        }

        for (name, directory) in [
            (c"MPLCONFIGDIR", matplotlib_directory),
            (c"XDG_CACHE_HOME", temporary_directory.join("cache")),
        ] {
            let directory = CString::new(directory.as_os_str().as_bytes())
                .expect("temporary directory should not contain NUL");
            set_environment(name, &directory, true)?;
        }
        Ok(())
    }

    pub(crate) fn link_matplotlib_caches() {
        let (Some(cache_directory), Some(directory)) = (
            INHERITED_MATPLOTLIB_DIRECTORY.get(),
            MATPLOTLIB_DIRECTORY.get(),
        ) else {
            return;
        };
        let Ok(caches) = fs::read_dir(cache_directory) else {
            return;
        };
        if fs::create_dir_all(directory).is_err() {
            return;
        }
        for cache in caches.flatten() {
            let name = cache.file_name();
            let Some(name) = name.to_str() else {
                continue;
            };
            if !name.starts_with("fontlist-v")
                || !name.ends_with(".json")
                || !cache.file_type().is_ok_and(|file_type| file_type.is_file())
            {
                continue;
            }
            let link = directory.join(name);
            if fs::symlink_metadata(&link).is_err() {
                let _ = symlink(cache.path(), link);
            }
        }
    }

    fn inherited_matplotlibrc(config_directory: Option<&Path>) -> Option<PathBuf> {
        if let Some(config) = std::env::var_os("MATPLOTLIBRC").filter(|path| !path.is_empty()) {
            let config = PathBuf::from(config);
            if let Some(config) =
                regular_file(&config).or_else(|| regular_file(&config.join("matplotlibrc")))
            {
                return Some(config);
            }
        }

        regular_file(&config_directory?.join("matplotlibrc"))
    }

    fn inherited_matplotlib_directory() -> Option<PathBuf> {
        let directory = match std::env::var_os("MPLCONFIGDIR") {
            Some(directory) if !directory.is_empty() => PathBuf::from(directory),
            Some(_) | None => {
                PathBuf::from(std::env::var_os("HOME").filter(|home| !home.is_empty())?)
                    .join(".matplotlib")
            }
        };
        if directory.is_absolute() {
            Some(directory)
        } else {
            Some(std::env::current_dir().ok()?.join(directory))
        }
    }

    fn regular_file(path: &Path) -> Option<PathBuf> {
        let path = path.canonicalize().ok()?;
        path.is_file().then_some(path)
    }

    fn set_environment(name: &CStr, value: &CStr, overwrite: bool) -> io::Result<()> {
        if unsafe { libc::setenv(name.as_ptr(), value.as_ptr(), overwrite.into()) } != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }

    #[allow(clippy::result_large_err)]
    #[harp::register]
    pub extern "C-unwind" fn mcp_console_publish_python_plot(data: SEXP) -> harp::Result<SEXP> {
        let data = String::try_from(harp::object::RObject::view(data))?;
        crate::worker::publish_plot(Ok(data));
        unsafe { Ok(libr::R_NilValue) }
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

    #[allow(clippy::result_large_err)]
    #[harp::register]
    pub extern "C-unwind" fn mcp_console_python_activated(
        requirements: SEXP,
    ) -> harp::Result<SEXP> {
        let requirements = String::try_from(harp::object::RObject::view(requirements))?;
        let requirements =
            serde_json::from_str(&requirements).map_err(|error| harp::anyhow!("{error}"))?;
        crate::worker::publish_python_activation(requirements)
            .map_err(|error| harp::anyhow!("{error}"))?;
        unsafe { Ok(libr::R_NilValue) }
    }

    #[allow(clippy::result_large_err)]
    #[harp::register]
    pub extern "C-unwind" fn mcp_console_resolve_python_version(
        request: SEXP,
    ) -> harp::Result<SEXP> {
        let request = String::try_from(harp::object::RObject::view(request))?;
        let request = serde_json::from_str(&request).map_err(|error| harp::anyhow!("{error}"))?;
        let version = crate::worker::resolve_python_version(request)
            .map_err(|error| harp::anyhow!("{error}"))?;
        Ok(harp::object::RObject::from(version).sexp)
    }
}

#[cfg(target_os = "macos")]
pub(crate) use platform::{
    Bridge, PreparationOutcome, configure_worker_environment, link_matplotlib_caches,
};
