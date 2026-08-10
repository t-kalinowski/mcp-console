#[cfg(target_os = "macos")]
mod platform {
    use std::ffi::{CStr, CString};
    use std::io;
    use std::os::unix::ffi::OsStrExt as _;

    use libr::SEXP;

    const BRIDGE_INIT: &str = r#"
base::local({
  evaluator <- NULL
  managed <- Sys.getenv("MCP_CONSOLE_MANAGED_PYTHON", unset = NA_character_)
  source <- NULL

  publish_images <- function(images) {
    for (image in images) {
      invisible(.Call("mcp_console_publish_python_plot", image))
    }
    invisible()
  }

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
import base64 as _base64
import builtins as _builtins
import io as _io
import logging as _logging
import sys as _sys
import traceback as _traceback


class _McpConsoleMatplotlibLogFilter(_logging.Filter):
    def filter(self, record):
        return record.getMessage() != (
            "Matplotlib is building the font cache; this may take a moment."
        )


_logging.getLogger("matplotlib.font_manager").addFilter(
    _McpConsoleMatplotlibLogFilter()
)


_mcp_console_matplotlib_images = []
_mcp_console_matplotlib_displayed = []


def _mcp_console_render_plots(
    figures,
    _BaseException=_builtins.BaseException,
    _base64=_base64,
    _displayed=_mcp_console_matplotlib_displayed,
    _io=_io,
    _print_exc=_traceback.print_exc,
):
    images = []
    for figure in figures:
        if any(displayed is figure for displayed in _displayed):
            continue
        _displayed.append(figure)
        try:
            output = _io.BytesIO()
            figure.savefig(output, format="png")
            data = output.getvalue()
            images.append(_base64.b64encode(data).decode("ascii"))
        except _BaseException:
            _print_exc()
    return images


def _mcp_console_matplotlib_figures(
    value,
    _displayed=_mcp_console_matplotlib_displayed,
    _isinstance=_builtins.isinstance,
    _list=_builtins.list,
    _sys=_sys,
    _tuple=_builtins.tuple,
):
    artist = _sys.modules.get("matplotlib.artist")
    container = _sys.modules.get("matplotlib.container")
    artist_type = getattr(artist, "Artist", ())
    container_type = getattr(container, "Container", ())
    if not artist_type and not container_type:
        return ()

    figures = []
    identities = set()

    def append_figure(candidate):
        if artist_type and _isinstance(candidate, artist_type):
            figure = candidate.get_figure()
            while figure is not None and not hasattr(figure, "savefig"):
                figure = figure.get_figure()
            if figure is not None and id(figure) not in identities:
                identities.add(id(figure))
                figures.append(figure)
            return
        if container_type and _isinstance(candidate, container_type):
            for item in candidate.get_children():
                append_figure(item)

    if (
        artist_type and _isinstance(value, artist_type)
        or container_type and _isinstance(value, container_type)
    ):
        append_figure(value)
    elif _isinstance(value, (_list, _tuple)):
        if not value:
            return ()
        append_figure(value[0])
        if len(value) > 1:
            append_figure(value[-1])
    else:
        append_figure(value)

    pyplot = _sys.modules.get("matplotlib.pyplot")
    managed = set()
    if pyplot is not None:
        for number in pyplot.get_fignums():
            managed.add(id(pyplot.figure(number)))
    return [
        figure
        for figure in figures
        if id(figure) in managed
        or any(displayed is figure for displayed in _displayed)
    ]


def _mcp_console_matplotlib_show(
    *args,
    _BaseException=_builtins.BaseException,
    _print_exc=_traceback.print_exc,
    _render=_mcp_console_render_plots,
    _sys=_sys,
    **kwargs,
):
    pyplot = _sys.modules["matplotlib.pyplot"]
    figures = []
    for number in sorted(pyplot.get_fignums()):
        try:
            figures.append(pyplot.figure(number))
        except _BaseException:
            _print_exc()
    images = _render(figures)
    try:
        pyplot.close("all")
    except _BaseException:
        _print_exc()
    return tuple(images)


def _mcp_console_finish_plots(
    _BaseException=_builtins.BaseException,
    _images=_mcp_console_matplotlib_images,
    _print_exc=_traceback.print_exc,
    _sys=_sys,
):
    pyplot = _sys.modules.get("matplotlib.pyplot")
    if pyplot is not None:
        try:
            pyplot.close("all")
        except _BaseException:
            _print_exc()
    return tuple(_images)


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
    _builtins_module=_builtins,
    _displayed=_mcp_console_matplotlib_displayed,
    _figures=_mcp_console_matplotlib_figures,
    _finish_plots=_mcp_console_finish_plots,
    _images=_mcp_console_matplotlib_images,
    _render=_mcp_console_render_plots,
    _sys=_sys,
    _print_exc=_traceback.print_exc,
):
    _images.clear()
    _displayed.clear()
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
            value = _eval(expression, _main.__dict__)
            figures = _figures(value)
            if figures:
                _builtins_module._ = value
                _images.extend(_render(figures))
            else:
                _sys.displayhook(value)
    except _BaseException:
        _print_exc()
    try:
        return _finish_plots()
    except _BaseException:
        _print_exc()
        return ()
)---", local = TRUE, convert = FALSE)
      reticulate::py_register_load_hook("matplotlib.pyplot", function() {
        pyplot <- reticulate::import("matplotlib.pyplot", convert = FALSE)
        pyplot$show <- function(...) {
          images <- reticulate::py_to_r(private$`_mcp_console_matplotlib_show`())
          publish_images(images)
          reticulate::py_none()
        }
      })
      evaluator <<- private$`_mcp_console_eval_cell`
    }

    filename <- paste0("<mcp-console:python:", id, ">")
    images <- reticulate::py_to_r(evaluator(source, filename))
    publish_images(images)
    invisible()
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
        for (name, value, overwrite) in [
            (c"RETICULATE_REMAP_OUTPUT_STREAMS", c"1", true),
            (c"UV_OFFLINE", c"1", true),
            (c"MPLBACKEND", c"agg", false),
        ] {
            set_environment(name, value, overwrite)?;
        }

        for (name, directory) in [
            (c"MPLCONFIGDIR", "matplotlib"),
            (c"XDG_CACHE_HOME", "cache"),
        ] {
            let directory = std::env::temp_dir().join(directory);
            let directory = CString::new(directory.as_os_str().as_bytes())
                .expect("temporary directory should not contain NUL");
            set_environment(name, &directory, true)?;
        }
        Ok(())
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
}

#[cfg(target_os = "macos")]
pub(crate) use platform::{Bridge, configure_worker_environment};
